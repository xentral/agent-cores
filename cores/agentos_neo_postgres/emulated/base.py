"""AgentOS Neo (standalone) — the Neo model persisted in a tenant-owned Postgres.

Outward this core is byte-compatible with the AgentOS Neo model (the schemas in
``model.json`` are a generated snapshot of the ``agentos_neo_xentral`` contract-2
documents — see ``scripts/generate_neo_postgres_model.py``). Inward there is no
ERP: every entity lives in the tenant's own Postgres as one table of JSONB
records in exactly the outward shape, so reads need no mapping at all.

Storage layout (per entity, created on demand):

    CREATE TABLE neo_<entity> (
      id         text PRIMARY KEY,          -- speaking id, e.g. so_1042
      data       jsonb NOT NULL,            -- the record, outward model shape
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    );

The schema bootstrap is additive-only and runs under a Postgres advisory lock,
so concurrent workers/first requests cannot race the DDL. New model fields need
no migration (JSONB); a new entity is one more ``CREATE TABLE IF NOT EXISTS``.

Connection config is resolved per tenant from the integration-accounts store
(``resolve_core_credentials``), the same lifecycle as the Odoo/Phoenix cores;
the gateway's Xentral ``base_url``/``token`` are ignored. Field paths used in
SQL come exclusively from the model schema (identifier whitelist); user input
only ever binds as parameters.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import Any

from entity_registry.core_sdk import (
    AdapterResponse,
    CoreCredentialsMissing,
    CredentialField,
    EmulationManifest,
    error_payload as credentials_error_payload,
    register_core_fields,
    resolve_core_credentials,
)
from datetime import UTC

logger = logging.getLogger(__name__)

CORE_ID = "agentos_neo_postgres"

# The per-tenant connection fields, stored as a first-party integration account
# under the ``agentos_neo_postgres`` connector. Names match the provider's
# credential_fields (see integrations.providers in the backend repo).
PG_FIELDS: tuple[CredentialField, ...] = (
    CredentialField("pg_host", example="db.example.com"),
    CredentialField("pg_port", example="5432", required=False),
    CredentialField("pg_database", example="agentos_neo_db"),
    CredentialField("pg_user", example="agentos_neo"),
    CredentialField("pg_password", example="…", secret=True),
    CredentialField("pg_sslmode", example="require", required=False),
)
register_core_fields(CORE_ID, PG_FIELDS)

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_PAGE_SIZE_DEFAULT = 25
_PAGE_SIZE_MAX = 100
_POOL_MIN, _POOL_MAX = 0, 5
_CONNECT_TIMEOUT = 10.0

# Test seam: when set, used instead of the tenant's stored connection.
_CONFIG_OVERRIDE: dict[str, str] | None = None

# One pool per (event loop, connection config); bootstrap memo per config.
_POOLS: dict[tuple, Any] = {}
_POOL_LOCKS: dict[int, asyncio.Lock] = {}
_BOOTSTRAPPED: dict[tuple, str] = {}

# our filter op -> SQL template fragments (value always binds as a parameter)
_TEXT_OPS = {
    "equals": "{e} = {p}",
    "notEquals": "{e} IS DISTINCT FROM {p}",
    "contains": "{e} ILIKE '%' || {p} || '%'",
    "notContains": "({e} IS NULL OR {e} NOT ILIKE '%' || {p} || '%')",
    "greaterThan": "{e} > {p}",
    "greaterThanOrEqual": "{e} >= {p}",
    "lessThan": "{e} < {p}",
    "lessThanOrEqual": "{e} <= {p}",
}
_NUMERIC_TYPES = frozenset({"number", "decimal", "integer"})


def snake(key: str) -> str:
    """SalesOrder -> sales_order (table-name component)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def _config() -> dict[str, str]:
    if _CONFIG_OVERRIDE is not None:
        return dict(_CONFIG_OVERRIDE)
    return resolve_core_credentials(CORE_ID, PG_FIELDS)


def _pool_key(cfg: dict[str, str]) -> tuple:
    return tuple(cfg.get(f.label) or "" for f in PG_FIELDS)


async def _get_pool(cfg: dict[str, str]):
    import asyncpg  # deferred: backend dependency, not needed at import time

    # asyncpg pools (and asyncio locks) are bound to their event loop, so the
    # cache is keyed per running loop. The server runs ONE long-lived loop, so
    # this stays a single pool per connection config in production; only
    # multi-loop contexts (tests, one-off scripts) get separate pools.
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, *_pool_key(cfg))
    pool = _POOLS.get(key)
    if pool is not None:
        return pool
    lock = _POOL_LOCKS.setdefault(loop_id, asyncio.Lock())
    async with lock:
        pool = _POOLS.get(key)
        if pool is not None:
            return pool

        async def _init(conn) -> None:
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

        sslmode = (cfg.get("pg_sslmode") or "").strip().lower()
        pool = await asyncpg.create_pool(
            host=cfg["pg_host"],
            port=int(cfg.get("pg_port") or 5432),
            database=cfg["pg_database"],
            user=cfg["pg_user"],
            password=cfg["pg_password"],
            ssl=True if sslmode in ("require", "verify-ca", "verify-full") else None,
            min_size=_POOL_MIN,
            max_size=_POOL_MAX,
            timeout=_CONNECT_TIMEOUT,
            init=_init,
        )
        _POOLS[key] = pool
        return pool


# --------------------------------------------------------------------------- #
# schema walk + SQL builders (pure, unit-testable)
# --------------------------------------------------------------------------- #


def field_index(properties: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Dotted path -> property spec, over top-level, embedded and collection
    subtrees (mirrors the facade's ``_resolve_path`` shape)."""
    out: dict[str, dict[str, Any]] = {}

    def walk(props: dict[str, Any], prefix: str) -> None:
        for name, spec in (props or {}).items():
            if not isinstance(spec, dict) or not _IDENT.match(name):
                continue
            path = f"{prefix}.{name}" if prefix else name
            out[path] = spec
            sub = spec.get("properties")
            if not isinstance(sub, dict):
                node = spec.get("node")
                sub = node.get("properties") if isinstance(node, dict) else None
            if isinstance(sub, dict):
                walk(sub, path)

    walk(properties, "")
    return out


def _jsonb_text(path: str) -> str:
    """``a.b`` -> ``data #>> '{a,b}'`` (parts are schema-validated identifiers)."""
    parts = path.split(".")
    return "data #>> '{" + ",".join(parts) + "}'"


def _typed_expr(path: str, spec: dict[str, Any]) -> str:
    base = _jsonb_text(path)
    if spec.get("type") in _NUMERIC_TYPES:
        # tolerate non-numeric garbage instead of erroring the whole query
        return f"(CASE WHEN {base} ~ '^-?[0-9.]+$' THEN ({base})::numeric END)"
    return base


def parse_query(query: list[tuple[str, str]]) -> dict[str, Any]:
    """The wire params (filter[i][key|op|value], sort, page[…], searchTerm)
    into a plain dict; unknown params are ignored (facade behavior)."""
    filters: dict[str, dict[str, str]] = {}
    out: dict[str, Any] = {
        "filters": [],
        "sort": None,
        "page": 1,
        "size": _PAGE_SIZE_DEFAULT,
        "search": None,
    }
    for k, v in query:
        m = re.match(r"^filter\[([0-9]+)\]\[(key|op|value)\]$", k)
        if m:
            filters.setdefault(m.group(1), {})[m.group(2)] = v
        elif k == "sort":
            out["sort"] = v
        elif k == "searchTerm":
            out["search"] = v
        elif k == "page[number]":
            try:
                out["page"] = max(1, int(v))
            except ValueError:
                pass
        elif k == "page[size]":
            try:
                out["size"] = max(1, min(_PAGE_SIZE_MAX, int(v)))
            except ValueError:
                pass
    for idx in sorted(filters, key=int):
        f = filters[idx]
        if f.get("key") and f.get("value") is not None:
            out["filters"].append(
                {"key": f["key"], "op": f.get("op") or "equals", "value": f["value"]}
            )
    return out


def build_list_query(
    table: str, index: dict[str, dict[str, Any]], parsed: dict[str, Any]
) -> tuple[str, list[Any]]:
    """(sql, args) for a list call. Field paths come from the schema index only;
    every value binds as a parameter. Raises ``ValueError`` with a user-facing
    message for an unknown field or operator."""
    where: list[str] = []
    args: list[Any] = []

    def bind(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    for f in parsed["filters"]:
        path, op, value = f["key"], f["op"], f["value"]
        spec = index.get(path)
        if spec is None:
            raise ValueError(f"unknown filter field '{path}'")
        ftype = spec.get("type")
        if ftype == "reference":
            expr = _jsonb_text(f"{path}.id")
        elif ftype == "tag":
            arr = "data #> '{" + ",".join(path.split(".")) + "}'"
            if op in ("equals", "in"):
                where.append(
                    f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(coalesce({arr}, '[]'::jsonb)) t(v) WHERE v = ANY({bind([s.strip() for s in value.split(',')])}))"
                )
                continue
            if op == "contains":
                where.append(
                    f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(coalesce({arr}, '[]'::jsonb)) t(v) WHERE v ILIKE '%' || {bind(value)} || '%')"
                )
                continue
            raise ValueError(f"operator '{op}' is not supported on tags")
        else:
            expr = _typed_expr(path, spec)
        if op == "in":
            vals = [s.strip() for s in str(value).split(",") if s.strip()]
            if spec.get("type") in _NUMERIC_TYPES:
                where.append(f"{expr} = ANY({bind([_num(v) for v in vals])}::numeric[])")
            else:
                where.append(f"{expr} = ANY({bind(vals)})")
            continue
        tpl = _TEXT_OPS.get(op)
        if tpl is None:
            raise ValueError(f"unknown filter operator '{op}'")
        value_arg: Any = value
        if spec.get("type") in _NUMERIC_TYPES and op != "contains":
            value_arg = _num(value)
            where.append(tpl.format(e=_typed_expr(path, spec), p=bind(value_arg) + "::numeric"))
            continue
        where.append(tpl.format(e=expr, p=bind(value_arg)))

    if parsed.get("search"):
        searchable = [
            p
            for p, s in index.items()
            if s.get("searchable") and "." not in p and s.get("type") in ("string", "select")
        ]
        if searchable:
            p = bind(parsed["search"])
            ors = [f"{_jsonb_text(path)} ILIKE '%' || {p} || '%'" for path in searchable]
            where.append("(" + " OR ".join(ors) + ")")

    order = "created_at DESC, id"
    if parsed.get("sort"):
        raw = parsed["sort"]
        desc = raw.startswith("-")
        path = raw[1:] if desc else raw
        # tolerate a caller echoing our own tiebreak back ("field,id")
        path = path.split(",")[0]
        spec = index.get(path)
        if spec is None and path != "id":
            raise ValueError(f"unknown sort field '{path}'")
        expr = "id" if path == "id" else _typed_expr(path, spec or {})
        order = f"{expr} {'DESC' if desc else 'ASC'} NULLS LAST, id"

    sql = f"SELECT data, count(*) OVER() AS _total FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    offset = (parsed["page"] - 1) * parsed["size"]
    sql += f" ORDER BY {order} LIMIT {int(parsed['size'])} OFFSET {int(offset)}"
    return sql, args


def _num(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{value}' is not a number") from exc


# --------------------------------------------------------------------------- #
# the adapter
# --------------------------------------------------------------------------- #


class PostgresEntityAdapter:
    """One Neo entity on the tenant's Postgres — synthesized from model.json."""

    def __init__(self, spec: dict[str, Any], model_version: str, all_tables: list[str]):
        self._spec = spec
        self._model_version = model_version
        self._all_tables = all_tables  # bootstrap creates every entity's table
        self.manifest = EmulationManifest(
            key=spec["key"],
            label_en=spec["label"],
            category=spec["category"],
            rollout_batch=CORE_ID,
            adapter=f"{CORE_ID}.{spec['key']}",
            source_apis=("postgres",),
            operations=tuple(spec["operations"]),
        )
        self._prefix: str = spec["idPrefix"]
        self._table = "neo_" + snake(spec["key"])
        self._seq = self._table + "_handle_seq"
        props = spec["metadata"]["rootNode"]["properties"]
        self._index = field_index(props)
        self._props = props

    # ---- schema ----------------------------------------------------------
    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        meta = copy.deepcopy(self._spec["metadata"])
        meta["origin"] = "emulated"
        meta["emulation"] = self.manifest.marker()
        return meta

    # ---- bootstrap --------------------------------------------------------
    async def _ensure_schema(self, pool, key: tuple) -> None:
        if _BOOTSTRAPPED.get(key) == self._model_version:
            return
        async with pool.acquire() as conn:
            # Serialize DDL across workers/processes; key is stable per core.
            await conn.execute("SELECT pg_advisory_lock(hashtext('agentos_neo_postgres'))")
            try:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS neo_meta (key text PRIMARY KEY, value jsonb NOT NULL)"
                )
                row = await conn.fetchrow("SELECT value FROM neo_meta WHERE key = 'model_version'")
                if row is None or row["value"] != self._model_version:
                    for table in self._all_tables:
                        await conn.execute(
                            f"""CREATE TABLE IF NOT EXISTS {table} (
                                  id text PRIMARY KEY,
                                  data jsonb NOT NULL,
                                  created_at timestamptz NOT NULL DEFAULT now(),
                                  updated_at timestamptz NOT NULL DEFAULT now())"""
                        )
                        await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {table}_handle_seq")
                        await conn.execute(
                            f"CREATE INDEX IF NOT EXISTS {table}_data_gin "
                            f"ON {table} USING gin (data jsonb_path_ops)"
                        )
                    await conn.execute(
                        """INSERT INTO neo_meta (key, value) VALUES ('model_version', $1::jsonb)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                        self._model_version,
                    )
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext('agentos_neo_postgres'))")
        _BOOTSTRAPPED[key] = self._model_version

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _json(status: int, payload: Any) -> AdapterResponse:
        return AdapterResponse(
            status, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"}
        )

    def _credentials_missing(self, exc: CoreCredentialsMissing) -> AdapterResponse:
        # 424: the upstream connection is a config gap, not a backend error.
        return self._json(
            424,
            {
                "title": "The AgentOS Neo database connection is not configured.",
                **credentials_error_payload(exc),
            },
        )

    def _writable_paths(self, *, creating: bool) -> set[str]:
        flag = "creatable" if creating else "updatable"
        return {
            name
            for name, spec in self._props.items()
            if isinstance(spec, dict) and spec.get(flag) and spec.get("access") != "readOnly"
        }

    def _required_paths(self) -> set[str]:
        return {
            name
            for name, spec in self._props.items()
            if isinstance(spec, dict) and "required" in (spec.get("rules") or [])
        }

    # ---- data --------------------------------------------------------------
    async def request(
        self,
        *,
        method: str,
        handle: str | None,
        query: list[tuple[str, str]],
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: Any = None,
    ) -> AdapterResponse:
        method = method.upper()
        required_op = {
            "GET": "read" if handle else "list",
            "POST": "create",
            "PATCH": "update",
            "PUT": "update",
            "DELETE": "delete",
        }.get(method)
        if required_op is None or required_op not in self.manifest.operations:
            return self._json(
                405,
                {
                    "title": f"{self.manifest.key}: '{required_op or method}' is not supported",
                    "detail": f"This entity declares only {sorted(self.manifest.operations)}.",
                },
            )
        try:
            cfg = _config()
        except CoreCredentialsMissing as exc:
            return self._credentials_missing(exc)
        try:
            pool = await _get_pool(cfg)
            await self._ensure_schema(pool, _pool_key(cfg))
            if method == "GET" and handle:
                return await self._read(pool, handle)
            if method == "GET":
                return await self._list(pool, query)
            if method == "POST":
                return await self._create(pool, body)
            if method in ("PATCH", "PUT"):
                return await self._update(pool, handle, body)
            return await self._delete(pool, handle)
        except Exception as exc:  # noqa: BLE001 — surface as a gateway error, never crash
            logger.warning("%s: postgres request failed: %s", CORE_ID, exc)
            return self._json(502, {"title": "Postgres request failed", "detail": str(exc)[:500]})

    async def _read(self, pool, handle: str) -> AdapterResponse:
        row = await pool.fetchrow(f"SELECT data FROM {self._table} WHERE id = $1", handle)
        if row is None:
            return self._json(404, {"title": f"{self.manifest.key} {handle} not found"})
        return self._json(200, {"data": row["data"]})

    async def _list(self, pool, query: list[tuple[str, str]]) -> AdapterResponse:
        parsed = parse_query(query)
        try:
            sql, args = build_list_query(self._table, self._index, parsed)
        except ValueError as exc:
            return self._json(400, {"title": str(exc)})
        rows = await pool.fetch(sql, *args)
        total = rows[0]["_total"] if rows else 0
        return self._json(200, {"data": [r["data"] for r in rows], "extra": {"total": total}})

    def _validate_write(
        self, body: bytes | None, *, creating: bool
    ) -> tuple[dict[str, Any] | None, AdapterResponse | None]:
        try:
            payload = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return None, self._json(400, {"title": "invalid JSON body"})
        if not isinstance(payload, dict):
            return None, self._json(400, {"title": "body must be a JSON object"})
        writable = self._writable_paths(creating=creating)
        rejected = sorted(k for k in payload if k not in writable)
        if rejected:
            return None, self._json(
                409,
                {
                    "title": f"{self.manifest.key}: fields not writable",
                    "detail": "These fields are read-only or unknown in the Neo model.",
                    "fields": rejected,
                },
            )
        if creating:
            missing = sorted(self._required_paths() - payload.keys())
            if missing:
                return None, self._json(
                    422,
                    {"title": f"{self.manifest.key}: required fields missing", "fields": missing},
                )
        return payload, None

    async def _create(self, pool, body: bytes | None) -> AdapterResponse:
        payload, err = self._validate_write(body, creating=True)
        if err:
            return err
        n = await pool.fetchval(f"SELECT nextval('{self._seq}')")
        record = dict(payload or {})
        record["id"] = f"{self._prefix}{n}"
        if "number" in self._props and not record.get("number"):
            record["number"] = str(n)
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        for stamp in ("createdAt", "updatedAt"):
            if stamp in self._props and not record.get(stamp):
                record[stamp] = now
        await pool.execute(
            f"INSERT INTO {self._table} (id, data) VALUES ($1, $2::jsonb)",
            record["id"],
            record,
        )
        return self._json(201, {"data": record})

    async def _update(self, pool, handle: str | None, body: bytes | None) -> AdapterResponse:
        if not handle:
            return self._json(400, {"title": "update needs a record id"})
        payload, err = self._validate_write(body, creating=False)
        if err:
            return err
        patch = dict(payload or {})
        if "updatedAt" in self._props:
            from datetime import datetime

            patch["updatedAt"] = datetime.now(UTC).isoformat()
        row = await pool.fetchrow(
            f"""UPDATE {self._table}
                SET data = data || $2::jsonb, updated_at = now()
                WHERE id = $1 RETURNING data""",
            handle,
            patch,
        )
        if row is None:
            return self._json(404, {"title": f"{self.manifest.key} {handle} not found"})
        return self._json(200, {"data": row["data"]})

    async def _delete(self, pool, handle: str | None) -> AdapterResponse:
        if not handle:
            return self._json(400, {"title": "delete needs a record id"})
        deleted = await pool.fetchval(
            f"DELETE FROM {self._table} WHERE id = $1 RETURNING id", handle
        )
        if deleted is None:
            return self._json(404, {"title": f"{self.manifest.key} {handle} not found"})
        return self._json(200, {"data": {"id": deleted}})

    # ---- actions -----------------------------------------------------------
    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: Any = None,
    ) -> AdapterResponse:
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if action_key in ("addTag", "removeTag"):
            return await self._tag_action(action_key, ids, envelope.get("command") or {})
        declared = {a.get("key") for a in self._spec["metadata"].get("actions") or []}
        if action_key in declared:
            return self._json(
                501,
                {
                    "title": f"{self.manifest.key}: '{action_key}' is not implemented yet",
                    "detail": "This action is part of the Neo model but not yet wired in the "
                    "standalone Postgres core.",
                },
            )
        return self._json(404, {"title": f"unknown action '{action_key}'"})

    async def _tag_action(
        self, action_key: str, ids: list[Any], command: dict[str, Any]
    ) -> AdapterResponse:
        title = str(command.get("title") or "").strip()
        if not title:
            return self._json(422, {"title": f"{action_key} requires a non-empty 'title'."})
        if not ids:
            return self._json(422, {"title": f"{action_key} needs a target id (ids[])"})
        handle = str(ids[0])
        try:
            cfg = _config()
        except CoreCredentialsMissing as exc:
            return self._credentials_missing(exc)
        try:
            pool = await _get_pool(cfg)
            await self._ensure_schema(pool, _pool_key(cfg))
            row = await pool.fetchrow(f"SELECT data FROM {self._table} WHERE id = $1", handle)
            if row is None:
                return self._json(404, {"title": f"{self.manifest.key} {handle} not found"})
            record = row["data"] or {}
            titles = [t for t in (record.get("tags") or []) if isinstance(t, str)]
            if action_key == "addTag":
                if title not in titles:
                    titles.append(title)
            else:
                titles = [t for t in titles if t != title]
            updated = await pool.fetchrow(
                f"""UPDATE {self._table} SET data = data || $2::jsonb, updated_at = now()
                    WHERE id = $1 RETURNING data""",
                handle,
                {"tags": titles},
            )
            return self._json(200, {"data": updated["data"] if updated else record})
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: tag action failed: %s", CORE_ID, exc)
            return self._json(502, {"title": "Postgres request failed", "detail": str(exc)[:500]})
