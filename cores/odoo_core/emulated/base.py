"""Odoo core — live-passthrough adapter base (mode C2).

Odoo speaks JSON-RPC (``/jsonrpc`` → ``execute_kw``), not our wire dialect, so
this adapter is a mode-C *translating* proxy: it fetches Odoo's own field
metadata (``fields_get``) and maps it into ``rootNode.properties``, and it
translates our ``filter[i]`` / ``sort`` / ``page`` params into an Odoo domain +
``search_read``. See ``docs/guides/building-an-erp-core.md`` → "Live passthrough
over an external backend".

Entities are *synthesized* from the live schema (see ``entities.py``): the
roster comes from ``ir.model``, scalars/references/collections from
``fields_get``. The declaration types below (``Entity``/``Field``/``Embed``/
``Collection``) are the intermediate contract the synthesis produces and this
base consumes. Embeds/collections are resolved with a single batched secondary
``read`` per relation.

An entity supports ``create`` and/or ``update`` when its ``operations`` (and,
for the dynamic adapter, the connected user's Odoo access rights) allow it,
with writable fields flagged per ``Field.writable``. Create (``POST``) calls
Odoo ``create``; update (``PATCH``/``PUT`` on a handle) calls Odoo ``write``.
Both accept only a whitelist from the wire body: plain writable scalars pass
through, and a writable ``many2one`` (``Field.ref``, targeting an in-roster
entity) is coerced from an id/reference to the Odoo record id. Writable
collections (``Collection.writable``) carry line items: on create they become
Odoo one2many ``(0, 0, {...})`` create tuples; on update they are synced by id
(update matches, create the idless, delete dropped lines). Embeds are never
written. Every other method — and any write to an entity that didn't opt in —
returns 405, and ``action()`` returns 405.

Connection config is resolved **per tenant from the vault**, not from the
gateway token — the gateway's ``base_url`` / ``token`` are the tenant's *Xentral*
auth, the wrong system here, so they are dropped. Nothing is hardcoded: the
tenant creates the vault entries (see ``ODOO_FIELDS``) and they are resolved via
:func:`resolve_core_credentials`. When they're missing the adapter returns the
shared "credentials missing" contract so the FE can prompt the user.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from ...credentials import (
    CoreCredentialsMissing,
    CredentialField,
    error_payload as credentials_error_payload,
    register_core_fields,
    resolve_core_credentials,
)

logger = logging.getLogger(__name__)

# The per-tenant connection fields, stored as a first-party integration account
# under the ``odoo_core`` connector (see integrations.providers.odoo_core). All four are
# required — Odoo authenticates JSON-RPC with the API key in place of the
# password. Field names match the provider's credential_fields ``name``s.
ODOO_FIELDS: tuple[CredentialField, ...] = (
    CredentialField("odoo_base_url", example="https://mein-odoo.example.com"),
    CredentialField("odoo_db", example="odoo_datenbankname"),
    CredentialField("odoo_login", example="benutzer@example.com"),
    CredentialField("odoo_api_key", example="f37166ec…", secret=True),
)
# Let the core selector discover Odoo's connection fields without importing this
# adapter module (it drives the "credentials missing" hint on the core card).
register_core_fields("odoo_core", ODOO_FIELDS)

_METADATA_TTL_SECONDS = 300.0
_TIMEOUT_SECONDS = 20.0

# (base, db, model) -> (fetched_at, fields_get payload). Keyed by instance, not
# just model, so one tenant's Odoo schema is never served to another.
_FIELDS_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_FIELDS_LOCK = threading.Lock()
# (base_url, db, login) -> uid
_UID_CACHE: dict[tuple[str, str, str], int] = {}
_UID_LOCK = threading.Lock()

# Odoo field type -> our render-contract `type`. many2one renders as its display
# string (records return `[id, name]`); modelling it as a `reference` would
# dangle unless the target entity is also in the roster.
_TYPE_MAP: dict[str, str] = {
    "char": "string",
    "text": "string",
    "html": "string",
    "integer": "integer",
    "float": "decimal",
    "monetary": "decimal",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "selection": "select",
    "many2one": "string",
}
# Types we can safely filter/sort on — but only when the Odoo field is *stored*
# (a non-stored computed field raises on domain search / order). many2one is
# excluded on purpose (see above); filtering/sorting is top-level only.
_QUERYABLE_TYPES = frozenset(
    {"char", "text", "integer", "float", "monetary", "boolean", "date", "datetime", "selection"}
)

# our filter op -> Odoo domain operator (1:1 cases; the rest are special-cased)
_OP_MAP: dict[str, str] = {
    "equals": "=",
    "notEquals": "!=",
    "contains": "ilike",
    "notContains": "not ilike",
    "greaterThan": ">",
    "greaterThanOrEqual": ">=",
    "lessThan": "<",
    "lessThanOrEqual": "<=",
}


# ---- entity declaration types (used by entities.py) -------------------------


@dataclass(frozen=True)
class Field:
    """One Odoo field. Top-level scalars keep ``key == odoo`` so filter/sort map
    straight to the column; nested (embed/collection) fields may be renamed.

    ``writable`` opts a scalar into the create/update payload: it renders
    without the ``access: readOnly`` marker and is accepted from the wire body on
    ``create``/``update`` — but *only* for an entity whose ``operations`` allow a
    write (see :meth:`OdooAdapterBase._supports_write`), so the flag is dormant on
    read-only entities that happen to share a field bundle. ``required`` adds a
    ``rules: ["required"]`` hint for the create/edit form.

    ``ref`` marks the field as a ``many2one`` relation whose target is the named
    in-roster entity key: it renders as a ``reference`` (id-based picker) instead
    of a plain scalar, reads back as ``{"id", "name"}`` instead of a flat display
    string, and — when also ``writable`` — accepts an id (scalar or ``{"id"}``)
    on write, coerced to the Odoo record id. The target must resolve any id it is
    pointed at (e.g. ``Contact`` has no base_domain, so it resolves any
    partner).

    ``preview`` >= 0 marks the field as a default list column at that position
    (rendered as ``previewable`` + ``previewOrder``); consumers show exactly the
    marked set. ``-1`` (default) leaves the field out of the default columns —
    when *no* field of the entity carries a preview position, the ``label_field``
    is marked previewable as the minimal fallback."""

    odoo: str
    section: str = "general"
    label: str = ""
    key: str = ""
    writable: bool = False
    required: bool = False
    ref: str = ""
    preview: int = -1

    def out_key(self) -> str:
        return self.key or self.odoo


@dataclass(frozen=True)
class Embed:
    """A nested object. ``source_field=None`` groups the record's *own* fields
    (e.g. a partner's address); otherwise it reads the record referenced by the
    ``source_field`` many2one from ``ref_model``."""

    key: str
    label: str
    fields: tuple[Field, ...]
    section: str = "general"
    source_field: str | None = None
    ref_model: str = ""

    @property
    def related(self) -> bool:
        return self.source_field is not None


@dataclass(frozen=True)
class Collection:
    """A repeating child list read from ``child_model`` via the one2many
    ``source_field`` on the main record.

    ``writable`` opts the list into writes (on a write-capable entity): each wire
    item's writable child fields drive an Odoo one2many command on
    ``source_field`` — a ``(0, 0, {...})`` create on the create path, and on
    update an id-matched sync (update / create / delete). Line ids round-trip via
    the item's ``id`` (added on read)."""

    key: str
    label: str
    source_field: str
    child_model: str
    fields: tuple[Field, ...]
    section: str = "general"
    writable: bool = False


@dataclass(frozen=True)
class Entity:
    key: str
    label_en: str
    category: str
    model: str
    label_field: str
    sections: tuple[tuple[str, str], ...]
    scalars: tuple[Field, ...]
    embeds: tuple[Embed, ...] = ()
    collections: tuple[Collection, ...] = ()
    base_domain: tuple[tuple[Any, ...], ...] = ()
    # Read-only by default; an entity opts into create by adding it here (and
    # flagging its writable scalars). Drives both the manifest and the 405 gate.
    operations: tuple[str, ...] = ("list", "read")
    # Odoo field values force-set on create so the new record lands inside this
    # entity's ``base_domain`` slice — e.g. a partner created via ``Customer``
    # needs ``customer_rank > 0`` to show in the customer list. Applied on create
    # only; an explicit value in the wire body still wins.
    create_defaults: tuple[tuple[str, Any], ...] = ()

    @property
    def has_preview_fields(self) -> bool:
        """Whether any scalar carries an explicit default-column position."""
        return any(f.preview >= 0 for f in self.scalars)


# ---- config + JSON-RPC plumbing ---------------------------------------------


class OdooRPCError(Exception):
    """An Odoo JSON-RPC ``error`` payload or a failed authentication."""


def _config() -> tuple[str, str, str, str]:
    """Resolve ``(base_url, db, login, api_key)`` from the tenant's Odoo
    integration account. Raises :class:`CoreCredentialsMissing` when none is
    configured."""
    creds = resolve_core_credentials("odoo_core", ODOO_FIELDS)
    return (
        creds["odoo_base_url"].rstrip("/"),
        creds["odoo_db"],
        creds["odoo_login"],
        creds["odoo_api_key"],
    )


def _rpc_error_message(err: Any) -> str:
    if isinstance(err, dict):
        data = err.get("data") or {}
        return str(data.get("message") or err.get("message") or err)
    return str(err)


def _rpc_payload(service: str, method: str, args: list[Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1,
    }


def _rpc_sync(base: str, service: str, method: str, args: list[Any]) -> Any:
    resp = httpx.post(
        f"{base}/jsonrpc",
        json=_rpc_payload(service, method, args),
        timeout=_TIMEOUT_SECONDS,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise OdooRPCError(_rpc_error_message(data["error"]))
    return data.get("result") if isinstance(data, dict) else None


def _uid_sync(base: str, db: str, login: str, secret: str) -> int:
    key = (base, db, login)
    with _UID_LOCK:
        cached = _UID_CACHE.get(key)
    if cached:
        return cached
    uid = _rpc_sync(base, "common", "authenticate", [db, login, secret, {}])
    if not isinstance(uid, int):
        raise OdooRPCError(f"authentication failed for {login}@{db}")
    with _UID_LOCK:
        _UID_CACHE[key] = uid
    return uid


def _fields_get(model: str) -> dict[str, Any]:
    """Odoo ``fields_get`` for a model, TTL-cached. Fetch failure serves the
    last good payload; only an unreachable-from-cold model raises."""
    base, db, login, secret = _config()
    cache_key = (base, db, model)
    now = time.monotonic()
    with _FIELDS_LOCK:
        cached = _FIELDS_CACHE.get(cache_key)
        if cached and now - cached[0] < _METADATA_TTL_SECONDS:
            return cached[1]
    try:
        uid = _uid_sync(base, db, login, secret)
        fields = _rpc_sync(
            base,
            "object",
            "execute_kw",
            [
                db,
                uid,
                secret,
                model,
                "fields_get",
                [],
                {
                    "attributes": [
                        "string",
                        "type",
                        "store",
                        "readonly",
                        "required",
                        "selection",
                        "relation",
                    ]
                },
            ],
        )
    except (httpx.HTTPError, OdooRPCError) as exc:
        with _FIELDS_LOCK:
            cached = _FIELDS_CACHE.get(cache_key)
        if cached:
            logger.warning("odoo: fields_get failed for %s, serving stale: %s", model, exc)
            return cached[1]
        raise
    if not isinstance(fields, dict):
        raise OdooRPCError(f"fields_get returned non-dict for {model}")
    with _FIELDS_LOCK:
        _FIELDS_CACHE[cache_key] = (now, fields)
    return fields


class OdooAdapterBase:
    """Live proxy to one Odoo model, driven by an :class:`Entity` declaration."""

    manifest: EmulationManifest
    entity: Entity

    # ---- schema -------------------------------------------------------------

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        e = self.entity
        try:
            main = _fields_get(e.model)
        except CoreCredentialsMissing as exc:
            return self._credentials_missing_metadata(exc)
        except (httpx.HTTPError, OdooRPCError) as exc:
            return self._unreachable_metadata(exc)
        properties: dict[str, Any] = {}
        for f in e.scalars:
            if f.odoo in main:
                properties[f.out_key()] = self._scalar_prop(f, main[f.odoo], nested=False)
        for emb in e.embeds:
            properties[emb.key] = self._embed_prop(emb, main)
        for col in e.collections:
            properties[col.key] = self._collection_prop(col)
        return {
            "key": e.key,
            "label": e.label_en,
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{" + e.label_field + "}}",
            "sections": {k: {"label": v} for k, v in e.sections}
            or {"general": {"label": "General"}},
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    def _scalar_prop(
        self, f: Field, meta: dict[str, Any], *, nested: bool, writable_container: bool = True
    ) -> dict[str, Any]:
        if f.ref:
            return self._reference_prop(f, meta, writable_container=writable_container)
        otype = meta.get("type")
        ptype = _TYPE_MAP.get(otype, "string")
        # Filtering/sorting is top-level only and needs a stored, queryable col.
        queryable = (not nested) and bool(meta.get("store")) and otype in _QUERYABLE_TYPES
        # Writable when the field opts in, the entity supports a write, and the
        # container is writable (a read-only embed keeps its fields read-only even
        # on a write-capable entity). Convention: writable == no `access`.
        writable = f.writable and self._supports_write() and writable_container
        # Default list columns: fields with an explicit preview position win;
        # only an entity with no marked field falls back to its label field.
        previewable = (not nested) and (
            f.preview >= 0
            or (not self.entity.has_preview_fields and f.out_key() == self.entity.label_field)
        )
        prop: dict[str, Any] = {
            "type": ptype,
            "label": f.label or meta.get("string") or f.odoo,
            "section": f.section,
            "filterable": queryable,
            "sortable": queryable,
            "searchable": False,
            "previewable": previewable,
        }
        if previewable and f.preview >= 0:
            prop["previewOrder"] = f.preview
        if not writable:
            prop["access"] = "readOnly"
        if writable and f.required:
            prop["rules"] = ["required"]
        if ptype == "select":
            prop["options"] = [
                {"value": v, "label": lbl} for v, lbl in (meta.get("selection") or [])
            ]
        if queryable:
            prop["filterOperators"] = _filter_operators(ptype)
        return prop

    def _reference_prop(
        self, f: Field, meta: dict[str, Any], *, writable_container: bool = True
    ) -> dict[str, Any]:
        """A many2one rendered as a reference to an in-roster entity. Not
        filter/sortable here (top-level scalar filtering only). Writable only on a
        write-capable entity + writable container; otherwise a read-only
        reference (works both top-level and inside a line-item node)."""
        writable = f.writable and self._supports_write() and writable_container
        previewable = f.preview >= 0
        prop: dict[str, Any] = {
            "type": "reference",
            "label": f.label or meta.get("string") or f.odoo,
            "section": f.section,
            "reference": f.ref,
            "renderProperty": "name",
            "filterable": False,
            "sortable": False,
            "searchable": False,
            "previewable": previewable,
        }
        if previewable:
            prop["previewOrder"] = f.preview
        if not writable:
            prop["access"] = "readOnly"
        if writable and f.required:
            prop["rules"] = ["required"]
        return prop

    def _sub_properties(
        self,
        fields: tuple[Field, ...],
        model_fields: dict[str, Any],
        *,
        writable_container: bool = False,
    ) -> dict[str, Any]:
        return {
            f.out_key(): self._scalar_prop(
                f, model_fields[f.odoo], nested=True, writable_container=writable_container
            )
            for f in fields
            if f.odoo in model_fields
        }

    def _embed_prop(self, emb: Embed, main: dict[str, Any]) -> dict[str, Any]:
        model_fields = _fields_get(emb.ref_model) if emb.related else main
        # Embeds are display-only (e.g. an address read from a related partner).
        return {
            "type": "embedded",
            "label": emb.label,
            "section": emb.section,
            "access": "readOnly",
            "properties": self._sub_properties(emb.fields, model_fields, writable_container=False),
        }

    def _collection_prop(self, col: Collection) -> dict[str, Any]:
        model_fields = _fields_get(col.child_model)
        writable = col.writable and self._supports_write()
        prop: dict[str, Any] = {
            "type": "collection",
            "label": col.label,
            "section": col.section,
            "node": {
                "properties": self._sub_properties(
                    col.fields, model_fields, writable_container=writable
                )
            },
        }
        if not writable:
            prop["access"] = "readOnly"
        return prop

    def _unreachable_metadata(self, exc: Exception) -> dict[str, Any]:
        e = self.entity
        # ``_config()`` can itself raise CoreCredentialsMissing — guard so the
        # host string never turns an unreachable error into an unhandled one.
        try:
            host = _config()[0]
        except CoreCredentialsMissing:
            host = "?"
        return {
            "key": e.key,
            "label": e.label_en,
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{" + e.label_field + "}}",
            "sections": {"general": {"label": "General"}},
            "rootNode": {"properties": {}},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
            "error": f"Odoo backend not reachable ({host}): {exc}",
        }

    def _empty_schema(self) -> dict[str, Any]:
        e = self.entity
        return {
            "key": e.key,
            "label": e.label_en,
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{" + e.label_field + "}}",
            "sections": {"general": {"label": "General"}},
            "rootNode": {"properties": {}},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    def _credentials_missing_metadata(self, exc: CoreCredentialsMissing) -> dict[str, Any]:
        """Valid-but-empty schema carrying the credentials-missing contract, so
        the FE renders the "connect this core" hint instead of an empty table."""
        schema = self._empty_schema()
        schema["error"] = "Odoo-Verbindung ist nicht konfiguriert."
        schema.update(credentials_error_payload(exc))
        return schema

    def _credentials_missing_response(self, exc: CoreCredentialsMissing) -> AdapterResponse:
        body = json.dumps(
            {"title": "Odoo-Verbindung ist nicht konfiguriert.", **credentials_error_payload(exc)},
            ensure_ascii=False,
        ).encode("utf-8")
        # 424 Failed Dependency: the request can't run until the tenant supplies
        # the upstream connection — a config gap, not a backend error (502).
        return AdapterResponse(424, body, {"content-type": "application/json"})

    # ---- data ---------------------------------------------------------------

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
        client: Any | None = None,
    ) -> AdapterResponse:
        del base_url, token, accept_language  # Odoo has its own host + auth.
        method_u = method.upper()
        # The only mutations this core allows, and only on an entity that opted
        # in: POST to the collection (create) and PATCH/PUT on a handle (update).
        # Everything else stays 405.
        is_create = method_u == "POST" and handle is None and self._supports_create()
        is_update = method_u in ("PATCH", "PUT") and handle is not None and self._supports_update()
        if method_u != "GET" and not is_create and not is_update:
            return self._status(405, "Write not permitted on this entity")
        owns = client is None
        conn = client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
        try:
            if is_create:
                return await self._create(conn, body)
            if is_update:
                return await self._update(conn, handle, body)
            if handle:
                return await self._read(conn, handle)
            return await self._list(conn, query)
        except CoreCredentialsMissing as exc:
            return self._credentials_missing_response(exc)
        except (httpx.HTTPError, OdooRPCError) as exc:
            return self._error(exc)
        finally:
            if owns:
                await conn.aclose()

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: Any | None = None,
    ) -> AdapterResponse:
        del action_key, handle, body, base_url, token, accept_language, client
        return self._status(405, "Odoo core exposes no actions")

    async def _list(self, conn: httpx.AsyncClient, query: list[tuple[str, str]]) -> AdapterResponse:
        e = self.entity
        domain, order, limit, offset = self._parse_query(query)
        full_domain = [*(list(c) for c in e.base_domain), *domain]
        total = await self._execute(conn, e.model, "search_count", [full_domain], {})
        read_kwargs: dict[str, Any] = {
            "fields": self._main_fields(),
            "limit": limit,
            "offset": offset,
        }
        if order:
            read_kwargs["order"] = order
        raw = await self._execute(conn, e.model, "search_read", [full_domain], read_kwargs)
        records = await self._compose(conn, raw or [])
        # The list view and the entity-tile record-count badge both read
        # ``meta.total``; ``extra.total`` is the documented gateway envelope.
        # Emit both so counts show and consumers reading either field agree.
        count = total if isinstance(total, int) else 0
        body = json.dumps(
            {
                "data": records,
                "meta": {"total": count, "count": len(records)},
                "extra": {"total": count},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return AdapterResponse(200, body, {"content-type": "application/json"})

    async def _read(self, conn: httpx.AsyncClient, handle: str) -> AdapterResponse:
        e = self.entity
        try:
            record_id = int(handle)
        except (TypeError, ValueError):
            return self._status(404, f"Not found: {handle}")
        domain = [*(list(c) for c in e.base_domain), ["id", "=", record_id]]
        raw = await self._execute(
            conn, e.model, "search_read", [domain], {"fields": self._main_fields(), "limit": 1}
        )
        if not raw:
            return self._status(404, f"Not found: {handle}")
        records = await self._compose(conn, raw)
        body = json.dumps({"data": records[0]}, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(200, body, {"content-type": "application/json"})

    def _supports_create(self) -> bool:
        return "create" in self.manifest.operations

    def _supports_update(self) -> bool:
        return "update" in self.manifest.operations

    def _supports_write(self) -> bool:
        return self._supports_create() or self._supports_update()

    async def _create(self, conn: httpx.AsyncClient, body: bytes | None) -> AdapterResponse:
        """Create one record from the wire body, then return it read-back.

        Create is the only write that accepts collections (line items): each
        writable collection becomes Odoo ``(0, 0, {...})`` create-command tuples
        on its ``source_field``.
        """
        e = self.entity
        payload, error = self._parse_body(body)
        if error is not None:
            return error
        scalars = self._scalar_values(payload)
        collections = self._collection_values(payload)
        if not scalars and not collections:
            return self._status(400, "No writable fields supplied")
        # Slice defaults first, so an explicit wire value still overrides them.
        values = {**dict(e.create_defaults), **scalars, **collections}
        new_id = await self._execute(conn, e.model, "create", [values], {})
        if not isinstance(new_id, int):
            raise OdooRPCError("create did not return a record id")
        created = await self._read(conn, str(new_id))
        # 201 Created on success; propagate any read-back error verbatim.
        if created.status_code == 200:
            return AdapterResponse(201, created.content, created.headers)
        return created

    async def _update(
        self, conn: httpx.AsyncClient, handle: str, body: bytes | None
    ) -> AdapterResponse:
        """Write the whitelisted scalars + line items onto an existing record.

        Line items are synced by id: a wire item with a known line id updates it
        (``(1, id, {...})``), one without creates (``(0, 0, {...})``), and any
        existing line the payload omits is deleted (``(2, id)``). A collection
        key absent from the body leaves those lines untouched; an empty list
        clears them. The target must fall inside this entity's ``base_domain`` —
        a record outside the slice is a 404, never a cross-slice write.
        """
        e = self.entity
        try:
            record_id = int(handle)
        except (TypeError, ValueError):
            return self._status(404, f"Not found: {handle}")
        payload, error = self._parse_body(body)
        if error is not None:
            return error
        domain = [*(list(c) for c in e.base_domain), ["id", "=", record_id]]
        if not await self._execute(conn, e.model, "search_count", [domain], {}):
            return self._status(404, f"Not found: {handle}")
        values = self._scalar_values(payload)
        values.update(await self._collection_sync(conn, record_id, payload))
        if not values:
            return self._status(400, "No writable fields supplied")
        await self._execute(conn, e.model, "write", [[record_id], values], {})
        return await self._read(conn, str(record_id))

    def _parse_body(
        self, body: bytes | None
    ) -> tuple[dict[str, Any], None] | tuple[None, AdapterResponse]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, self._status(400, f"Invalid JSON body: {exc}")
        if not isinstance(payload, dict):
            return None, self._status(400, "Body must be a JSON object")
        return payload, None

    def _scalar_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Whitelisted writable scalars from the wire body. An unknown or
        read-only key is ignored, never forwarded. A plain scalar passes through;
        a ``ref`` (relation) field is coerced from its wire form (id, numeric
        string, or ``{"id"}``) to the Odoo record id."""
        values: dict[str, Any] = {}
        for f in self.entity.scalars:
            if not (f.writable and f.out_key() in payload):
                continue
            raw = payload[f.out_key()]
            values[f.odoo] = _coerce_ref(raw) if f.ref else raw
        return values

    def _line_values(self, col: Collection, item: dict[str, Any]) -> dict[str, Any]:
        """One wire line item → Odoo child values (whitelisted writable child
        fields; plain pass-through, ``ref`` coerced to id)."""
        line: dict[str, Any] = {}
        for f in col.fields:
            if f.writable and f.out_key() in item:
                raw = item[f.out_key()]
                line[f.odoo] = _coerce_ref(raw) if f.ref else raw
        return line

    def _collection_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Writable collections → Odoo one2many ``(0, 0, {...})`` create tuples
        (create path). An empty line item contributes nothing."""
        out: dict[str, Any] = {}
        for col in self.entity.collections:
            if not col.writable:
                continue
            items = payload.get(col.key)
            if not isinstance(items, list):
                continue
            tuples = [
                [0, 0, line]
                for item in items
                if isinstance(item, dict) and (line := self._line_values(col, item))
            ]
            if tuples:
                out[col.source_field] = tuples
        return out

    async def _collection_sync(
        self, conn: httpx.AsyncClient, record_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Writable collections → Odoo one2many *sync* commands (update path):
        match wire items to existing lines by id — update matches, create the
        idless, delete existing lines the payload dropped. A collection absent
        from the body is left untouched; an empty list clears it."""
        e = self.entity
        active = [
            col for col in e.collections if col.writable and isinstance(payload.get(col.key), list)
        ]
        if not active:
            return {}
        current = await self._execute(
            conn, e.model, "read", [[record_id]], {"fields": [c.source_field for c in active]}
        )
        existing_by_field = {
            c.source_field: set(current[0].get(c.source_field) or []) for c in active
        }
        out: dict[str, Any] = {}
        for col in active:
            existing = existing_by_field[col.source_field]
            commands: list[Any] = []
            seen: set[int] = set()
            for item in payload[col.key]:
                if not isinstance(item, dict):
                    continue
                line = self._line_values(col, item)
                iid = _as_int(item.get("id"))
                if iid is not None and iid in existing:
                    seen.add(iid)
                    if line:  # nothing to change → skip a no-op update command
                        commands.append([1, iid, line])
                elif line:
                    commands.append([0, 0, line])
            commands.extend([2, eid] for eid in sorted(existing - seen))
            if commands:
                out[col.source_field] = commands
        return out

    def _main_fields(self) -> list[str]:
        """Odoo columns to pull on the main model: scalars, local-embed source
        fields, related-embed pointers, and collection pointers — intersected
        with the live schema so a stale curated name never 400s the read."""
        e = self.entity
        available = _fields_get(e.model)
        wanted: list[str] = [f.odoo for f in e.scalars]
        for emb in e.embeds:
            if emb.related and emb.source_field:
                wanted.append(emb.source_field)
            else:
                wanted.extend(f.odoo for f in emb.fields)
        wanted.extend(col.source_field for col in e.collections)
        seen: set[str] = set()
        return [f for f in wanted if f in available and not (f in seen or seen.add(f))]

    async def _compose(
        self, conn: httpx.AsyncClient, raw: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Turn raw Odoo rows into wire records, resolving embeds + collections
        with one batched secondary read per relation."""
        e = self.entity
        main_types = _model_types(e.model)
        embed_data: dict[str, dict[int, dict[str, Any]]] = {}
        for emb in e.embeds:
            if emb.related and emb.source_field:
                embed_data[emb.key] = await self._resolve_related(conn, raw, emb)
        collection_data: dict[str, dict[int, dict[str, Any]]] = {}
        for col in e.collections:
            collection_data[col.key] = await self._resolve_collection(conn, raw, col)

        out: list[dict[str, Any]] = []
        for row in raw:
            record = _transform(row, e.scalars, main_types)
            for emb in e.embeds:
                if emb.related and emb.source_field:
                    ref = _ref_id(row.get(emb.source_field))
                    record[emb.key] = embed_data[emb.key].get(ref, {}) if ref else {}
                else:
                    record[emb.key] = _transform(row, emb.fields, main_types, with_id=False)
            for col in e.collections:
                child_map = collection_data[col.key]
                ids = row.get(col.source_field) or []
                record[col.key] = [child_map[cid] for cid in ids if cid in child_map]
            rid = str(row.get("id"))
            record["id"] = rid
            record["uuid"] = rid
            out.append(record)
        return out

    async def _resolve_related(
        self, conn: httpx.AsyncClient, raw: list[dict[str, Any]], emb: Embed
    ) -> dict[int, dict[str, Any]]:
        ids = sorted({rid for row in raw if (rid := _ref_id(row.get(emb.source_field)))})
        if not ids:
            return {}
        fields = _existing(emb.ref_model, [f.odoo for f in emb.fields])
        rows = await self._execute(conn, emb.ref_model, "read", [ids], {"fields": fields})
        types = _model_types(emb.ref_model)
        return {row["id"]: _transform(row, emb.fields, types) for row in (rows or [])}

    async def _resolve_collection(
        self, conn: httpx.AsyncClient, raw: list[dict[str, Any]], col: Collection
    ) -> dict[int, dict[str, Any]]:
        ids = sorted({cid for row in raw for cid in (row.get(col.source_field) or [])})
        if not ids:
            return {}
        fields = _existing(col.child_model, [f.odoo for f in col.fields])
        rows = await self._execute(conn, col.child_model, "read", [ids], {"fields": fields})
        types = _model_types(col.child_model)
        return {row["id"]: _transform(row, col.fields, types) for row in (rows or [])}

    # ---- query translation --------------------------------------------------

    def _parse_query(
        self, query: list[tuple[str, str]]
    ) -> tuple[list[list[Any]], str | None, int, int]:
        raw_filters: dict[str, dict[str, str]] = {}
        sort: str | None = None
        limit = 50
        page = 1
        for key, value in query:
            # Accept both the bracketed (`page[size]`) and plain (`perPage`)
            # forms — the list view sends the former, the count-badge query the
            # latter, and honouring `perPage=1` keeps the count query to 1 row.
            if key in ("page[size]", "perPage"):
                limit = _clamp_int(value, default=limit, lo=1, hi=200)
            elif key in ("page[number]", "page"):
                page = _clamp_int(value, default=page, lo=1, hi=1_000_000)
            elif key == "sort":
                sort = value
            elif key.startswith("filter[") and "][" in key:
                idx = key[len("filter[") : key.index("]")]
                part = key[key.index("][") + 2 : -1]
                raw_filters.setdefault(idx, {})[part] = value
        domain: list[list[Any]] = []
        for spec in raw_filters.values():
            clause = self._filter_clause(
                spec.get("key"), spec.get("op") or "equals", spec.get("value", "")
            )
            if clause:
                domain.append(clause)
        return domain, self._order(sort), limit, (page - 1) * limit

    def _scalar_names(self) -> set[str]:
        return {f.odoo for f in self.entity.scalars}

    def _order(self, sort: str | None) -> str | None:
        if not sort:
            return None
        desc = sort.startswith("-")
        field_name = sort[1:] if desc else sort
        if field_name not in self._scalar_names():
            return None
        return f"{field_name} {'desc' if desc else 'asc'}"

    def _filter_clause(self, key: str | None, op: str, value: str) -> list[Any] | None:
        if not key or key not in self._scalar_names():
            return None
        otype = _model_types(self.entity.model).get(key)
        if otype is None or otype not in _QUERYABLE_TYPES:
            return None
        return _clause(key, op, value, otype)

    # ---- rpc + responses ----------------------------------------------------

    async def _execute(
        self,
        conn: httpx.AsyncClient,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        base, db, login, secret = _config()
        uid = await self._uid(conn, base, db, login, secret)
        return await self._rpc(
            conn, base, "object", "execute_kw", [db, uid, secret, model, method, args, kwargs]
        )

    async def _uid(
        self, conn: httpx.AsyncClient, base: str, db: str, login: str, secret: str
    ) -> int:
        cache_key = (base, db, login)
        with _UID_LOCK:
            cached = _UID_CACHE.get(cache_key)
        if cached:
            return cached
        uid = await self._rpc(conn, base, "common", "authenticate", [db, login, secret, {}])
        if not isinstance(uid, int):
            raise OdooRPCError(f"authentication failed for {login}@{db}")
        with _UID_LOCK:
            _UID_CACHE[cache_key] = uid
        return uid

    @staticmethod
    async def _rpc(
        conn: httpx.AsyncClient, base: str, service: str, method: str, args: list[Any]
    ) -> Any:
        resp = await conn.post(
            f"{base}/jsonrpc",
            json=_rpc_payload(service, method, args),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise OdooRPCError(_rpc_error_message(data["error"]))
        return data.get("result") if isinstance(data, dict) else None

    def _error(self, exc: Exception) -> AdapterResponse:
        logger.warning("odoo: request failed for %s: %s", self.entity.key, exc)
        body = json.dumps({"title": f"Odoo backend error: {exc}"}).encode("utf-8")
        return AdapterResponse(502, body, {"content-type": "application/json"})

    @staticmethod
    def _status(code: int, message: str) -> AdapterResponse:
        body = json.dumps({"title": message}).encode("utf-8")
        return AdapterResponse(code, body, {"content-type": "application/json"})


# ---- module helpers ---------------------------------------------------------


def _model_types(model: str) -> dict[str, str]:
    try:
        fields = _fields_get(model)
    except (httpx.HTTPError, OdooRPCError):
        return {}
    return {name: meta.get("type") for name, meta in fields.items()}


def _existing(model: str, names: list[str]) -> list[str]:
    try:
        available = _fields_get(model)
    except (httpx.HTTPError, OdooRPCError):
        return names
    return [n for n in names if n in available]


def _ref_id(value: Any) -> int | None:
    """Odoo many2one arrives as ``[id, name]`` (or ``False``)."""
    if isinstance(value, list) and value and isinstance(value[0], int):
        return value[0]
    return None


def _coerce_ref(value: Any) -> int | bool:
    """Wire relation value → Odoo id, or ``False`` to clear. Accepts a bare id, a
    numeric string, or a ``{"id": ...}`` reference object; anything unparseable
    (incl. a display-name string — names are not resolved here) clears it."""
    if isinstance(value, dict):
        value = value.get("id")
    if value in (None, "", False):
        return False
    try:
        return int(value)
    except (TypeError, ValueError):
        return False


def _as_int(value: Any) -> int | None:
    """Parse a wire line-item id (int or numeric string) to int, else None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _transform(
    raw: dict[str, Any], fields: tuple[Field, ...], types: dict[str, str], *, with_id: bool = True
) -> dict[str, Any]:
    """Odoo row → wire record for the given fields. A ``ref`` (relation) field
    keeps its id — ``[id, name]`` → ``{"id", "name"}`` (empty ``False`` → null);
    any other many2one collapses to its display name; Odoo's empty-value
    ``False`` becomes ``null`` (except real booleans)."""
    out: dict[str, Any] = {}
    for f in fields:
        if f.odoo not in raw:
            continue
        value = raw[f.odoo]
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
            out[f.out_key()] = {"id": str(value[0]), "name": value[1]} if f.ref else value[1]
        elif f.ref and value is False:
            out[f.out_key()] = None
        elif isinstance(value, list):
            out[f.out_key()] = value  # e.g. an unresolved id list — keep as-is
        elif value is False and types.get(f.odoo) != "boolean":
            out[f.out_key()] = None
        else:
            out[f.out_key()] = value
    if with_id and "id" in raw:
        rid = str(raw["id"])
        out["id"] = rid
        out["uuid"] = rid
    return out


def _filter_operators(ptype: str) -> list[str]:
    if ptype == "select":
        return ["equals", "notEquals", "in"]
    if ptype in ("integer", "decimal"):
        return ["equals", "notEquals", "greaterThan", "lessThan"]
    if ptype in ("date", "datetime"):
        return ["equals", "greaterThan", "lessThan"]
    if ptype == "boolean":
        return ["equals"]
    return ["equals", "contains", "startsWith"]


def _clause(key: str, op: str, value: str, otype: str) -> list[Any]:
    if op in ("in", "notIn"):
        values = [_coerce(v.strip(), otype) for v in str(value).split(",") if v.strip() != ""]
        return [key, "in" if op == "in" else "not in", values]
    if op == "isNull":
        return [key, "=", False]
    if op == "isNotNull":
        return [key, "!=", False]
    if op == "startsWith":
        return [key, "=ilike", f"{value}%"]
    if op == "endsWith":
        return [key, "=ilike", f"%{value}"]
    return [key, _OP_MAP.get(op, "="), _coerce(value, otype)]


def _clamp_int(value: str, *, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _coerce(value: str, otype: str) -> Any:
    if otype == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if otype in ("float", "monetary"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if otype == "boolean":
        return str(value).strip().lower() in ("1", "true", "yes")
    return value
