"""weclapp REST transport + adapter engine for the AgentOS Neo (weclapp) core.

Mode-C2: weclapp speaks a REST/JSON API, not our wire dialect, so this module
*translates* at the boundary. Unlike ``odoo_core`` it does NOT introspect the
schema live (weclapp has no ``fields_get`` equivalent) — each entity is described
by a static :class:`Entity` declaration (see ``entities.py``), so ``metadata()``
renders offline from the declaration and only the data path touches the tenant.

Layers, from the bottom:

1. Connection — ``WECLAPP_FIELDS`` + ``register_core_fields`` + ``WeclappClient``
   (the ``AuthenticationToken`` header, the ``/webapp/api/v1`` root, the
   ``/<entity>`` · ``/<entity>/id/{id}`` · ``/<entity>/count`` routes).
2. Declaration types — ``Field`` / ``Reference`` / ``Embed`` / ``Collection`` /
   ``Entity``: what an entity exposes and how each field maps to a weclapp
   property.
3. Pure translation helpers — ``parse_query`` / ``build_list_params`` /
   ``transform_record``: no network, unit-tested in isolation.
4. ``WeclappAdapterBase`` — the ``EmulatedEntityAdapter`` (metadata/request/action)
   that wires 1–3 into our envelope contract.

NOTE: weclapp property names in the declarations are provisional and MUST be
reconciled against the tenant's live OpenAPI/Swagger before an entity is "done"
(see ``../docs/00-concept.md`` — the two facts sourced from SDKs rather than
weclapp's own prose). The translation logic below is tenant-independent and is
what the unit tests pin down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from entity_registry.core_sdk import (
    AdapterResponse,
    CoreCredentialsMissing,
    CredentialField,
    EmulationManifest,
    error_payload as credentials_error_payload,
    register_core_fields,
    resolve_core_credentials,
)

logger = logging.getLogger(__name__)

CORE_ID = "agentos_neo_weclapp"

# weclapp REST API root, appended to the stored base URL if the tenant pasted only
# the instance host. weclapp exposes v1 and v2; v1 is used here.
_API_ROOT = "/webapp/api/v1"
_TIMEOUT_SECONDS = 20.0
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100

# Per-tenant connection fields, stored as a first-party integration account under
# the ``agentos_neo_weclapp`` connector (see integrations.providers). weclapp
# authenticates with a static API token in the ``AuthenticationToken`` header.
WECLAPP_FIELDS: tuple[CredentialField, ...] = (
    CredentialField("weclapp_base_url", example="https://mycompany.weclapp.com"),
    CredentialField("weclapp_api_token", example="a1b2c3d4…", secret=True),
)
# Let the core selector discover weclapp's connection fields without importing this
# adapter module (drives the "credentials missing" hint on the core card).
register_core_fields(CORE_ID, WECLAPP_FIELDS)


def _api_base(stored_base_url: str) -> str:
    """The full API root for a stored base URL. Accepts either the bare instance
    host or a value already including ``/webapp/api/v1``; never trailing-slashed."""
    base = (stored_base_url or "").strip().rstrip("/")
    return base if base.endswith(_API_ROOT) else f"{base}{_API_ROOT}"


# ---- connection client ------------------------------------------------------


class WeclappClient:
    """Thin async REST client over a tenant's weclapp instance. Credentials are
    resolved lazily and raise :class:`CoreCredentialsMissing` when unconfigured."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, *, connector_id: str = CORE_ID
    ) -> None:
        self._client = client
        # Every core resolves its OWN connector — nothing shares a connection.
        self._connector_id = connector_id
        self._base: str | None = None
        self._token: str | None = None

    def _resolve(self) -> tuple[str, str]:
        if self._base is None or self._token is None:
            creds = resolve_core_credentials(self._connector_id, WECLAPP_FIELDS)
            self._base = _api_base(creds["weclapp_base_url"])
            self._token = creds["weclapp_api_token"]
        return self._base, self._token

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None
    ) -> httpx.Response:
        base, token = self._resolve()
        url = f"{base}/{path.lstrip('/')}"
        headers = {"AuthenticationToken": token, "Accept": "application/json"}
        if self._client is not None:
            resp = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as conn:
                resp = await conn.request(
                    method, url, params=params, json=json_body, headers=headers
                )
        resp.raise_for_status()
        return resp

    async def list(self, entity: str, *, params: dict | None = None) -> Any:
        return _unwrap((await self._request("GET", entity, params=params)).json())

    async def get(self, entity: str, record_id: str, *, params: dict | None = None) -> Any:
        return _unwrap(
            (await self._request("GET", f"{entity}/id/{record_id}", params=params)).json()
        )

    async def count(self, entity: str, *, params: dict | None = None) -> int:
        data = _unwrap((await self._request("GET", f"{entity}/count", params=params)).json())
        try:
            return int(data)
        except (TypeError, ValueError):
            return 0

    async def create(self, entity: str, payload: dict) -> Any:
        return _unwrap((await self._request("POST", entity, json_body=payload)).json())

    async def update(self, entity: str, record_id: str, payload: dict) -> Any:
        return _unwrap(
            (await self._request("PUT", f"{entity}/id/{record_id}", json_body=payload)).json()
        )

    async def delete(self, entity: str, record_id: str) -> None:
        await self._request("DELETE", f"{entity}/id/{record_id}")


def _unwrap(payload: Any) -> Any:
    """weclapp wraps collection/scalar responses as ``{"result": …}``; a single
    entity may come back bare. Return the inner value when wrapped."""
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


# ---- declaration types ------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One scalar weclapp property. ``wire`` is the weclapp property name; ``key``
    (defaults to ``wire``) is the outward field name. ``type`` is our render
    vocabulary. ``epoch`` marks a weclapp Unix-epoch-milliseconds timestamp to
    render as an ISO ``date``/``datetime``. ``options`` populate a ``select``."""

    wire: str
    label: str = ""
    type: str = "string"
    section: str = "general"
    key: str = ""
    filterable: bool = False
    sortable: bool = False
    searchable: bool = False
    writable: bool = False
    required: bool = False
    epoch: bool = False
    preview: int = -1
    options: tuple[tuple[str, str], ...] = ()

    def out_key(self) -> str:
        return self.key or self.wire


@dataclass(frozen=True)
class Reference:
    """A weclapp ``<entity>Id`` foreign key rendered as a reference to the named
    in-core entity. weclapp returns only the id string, so the read serializes as
    ``{"id": <fk>}`` (the FE falls back to the id when the display name is absent
    unless ``render_property`` is resolved via ``additional_properties``)."""

    wire: str
    label: str
    reference: str
    section: str = "general"
    key: str = ""
    render_property: str = "id"
    writable: bool = False
    required: bool = False
    preview: int = -1

    def out_key(self) -> str:
        return self.key or self.wire


@dataclass(frozen=True)
class Embed:
    """A nested weclapp object (e.g. ``recordAddress``) exposed as an embedded
    node. Read-only in Phase 1."""

    wire: str
    label: str
    fields: tuple[Any, ...]
    section: str = "general"
    key: str = ""

    def out_key(self) -> str:
        return self.key or self.wire


@dataclass(frozen=True)
class Collection:
    """A repeating weclapp child list embedded on the record (e.g. ``orderItems``,
    ``addresses``). Read-only in Phase 1. Some weclapp lists are only returned when
    requested via ``additionalProperties`` — declare those on the entity."""

    wire: str
    label: str
    fields: tuple[Any, ...]
    section: str = "general"
    key: str = ""

    def out_key(self) -> str:
        return self.key or self.wire


@dataclass(frozen=True)
class Entity:
    key: str
    label_en: str
    category: str
    endpoint: str
    label_field: str
    sections: tuple[tuple[str, str], ...]
    scalars: tuple[Field, ...]
    references: tuple[Reference, ...] = ()
    embeds: tuple[Embed, ...] = ()
    collections: tuple[Collection, ...] = ()
    operations: tuple[str, ...] = ("list", "read")
    # weclapp query params force-applied to every list/count so the endpoint is
    # sliced to this logical entity — e.g. Customer over the polymorphic ``/party``
    # sets ``customer-eq=true``. Applied to both count and list.
    base_params: tuple[tuple[str, str], ...] = ()
    # weclapp properties that are only returned when explicitly requested (embedded
    # collections like ``orderItems``), passed as ``additionalProperties``.
    additional_properties: tuple[str, ...] = ()

    @property
    def has_preview_fields(self) -> bool:
        return any(getattr(f, "preview", -1) >= 0 for f in (*self.scalars, *self.references))


# ---- pure translation helpers (no network) ----------------------------------

# our filter op -> weclapp operator suffix (1:1 cases). LIKE-family ops carry a
# wildcard transform on the value and are handled in build_list_params.
_OP_SUFFIX: dict[str, str] = {
    "equals": "eq",
    "notEquals": "ne",
    "greaterThan": "gt",
    "greaterThanOrEqual": "ge",
    "lessThan": "lt",
    "lessThanOrEqual": "le",
    "in": "in",
    "notIn": "notin",
    "isNull": "null",
    "isNotNull": "notnull",
}
# LIKE-family: (suffix, wildcard-wrapped value builder).
_LIKE_OPS: dict[str, Any] = {
    "contains": lambda v: ("ilike", f"%{v}%"),
    "startsWith": lambda v: ("ilike", f"{v}%"),
    "endsWith": lambda v: ("ilike", f"%{v}"),
}
_VALUELESS_OPS = frozenset({"isNull", "isNotNull"})

_FILTER_OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "string": ("equals", "notEquals", "contains", "startsWith", "endsWith", "isNull", "isNotNull"),
    "select": ("equals", "notEquals", "in", "notIn", "isNull", "isNotNull"),
    "integer": (
        "equals",
        "notEquals",
        "greaterThan",
        "greaterThanOrEqual",
        "lessThan",
        "lessThanOrEqual",
    ),
    "decimal": (
        "equals",
        "notEquals",
        "greaterThan",
        "greaterThanOrEqual",
        "lessThan",
        "lessThanOrEqual",
    ),
    "boolean": ("equals", "notEquals"),
    "date": ("equals", "greaterThan", "greaterThanOrEqual", "lessThan", "lessThanOrEqual"),
    "datetime": ("equals", "greaterThan", "greaterThanOrEqual", "lessThan", "lessThanOrEqual"),
}


@dataclass(frozen=True)
class ParsedQuery:
    filters: tuple[tuple[str, str, str], ...]  # (key, op, value)
    sort: str | None
    page: int
    page_size: int


def _clamp_int(value: str, *, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def parse_query(query: list[tuple[str, str]]) -> ParsedQuery:
    """Parse our wire query (``filter[i][key|op|value]`` / ``sort`` /
    ``page[size|number]`` or ``perPage``/``page``) into a neutral shape."""
    raw_filters: dict[str, dict[str, str]] = {}
    sort: str | None = None
    page_size = _DEFAULT_PAGE_SIZE
    page = 1
    for key, value in query:
        if key in ("page[size]", "perPage"):
            page_size = _clamp_int(value, default=page_size, lo=1, hi=_MAX_PAGE_SIZE)
        elif key in ("page[number]", "page"):
            page = _clamp_int(value, default=page, lo=1, hi=1_000_000)
        elif key == "sort":
            sort = value or None
        elif key.startswith("filter[") and "][" in key:
            idx = key[len("filter[") : key.index("]")]
            part = key[key.index("][") + 2 : -1]
            raw_filters.setdefault(idx, {})[part] = value
    filters = tuple(
        (spec.get("key", ""), spec.get("op") or "equals", spec.get("value", ""))
        for spec in raw_filters.values()
        if spec.get("key")
    )
    return ParsedQuery(filters=filters, sort=sort, page=page, page_size=page_size)


def _query_index(entity: Entity) -> dict[str, Field]:
    """out_key -> Field for the filterable/sortable scalars (top-level only)."""
    return {f.out_key(): f for f in entity.scalars}


def build_list_params(
    entity: Entity, parsed: ParsedQuery, *, for_count: bool = False
) -> dict[str, str]:
    """weclapp querystring params for a list/count call: base slice + translated
    filters (+ sort/pagination for a list)."""
    params: dict[str, str] = dict(entity.base_params)
    index = _query_index(entity)
    for key, op, value in parsed.filters:
        f = index.get(key)
        if f is None or not f.filterable:
            continue  # unknown or non-filterable key is dropped, never forwarded
        if op in _VALUELESS_OPS:
            params[f"{f.wire}-{_OP_SUFFIX[op]}"] = ""
        elif op in _LIKE_OPS and f.type in ("string", "select"):
            suffix, wrapped = _LIKE_OPS[op](value)
            params[f"{f.wire}-{suffix}"] = wrapped
        elif op in _OP_SUFFIX:
            params[f"{f.wire}-{_OP_SUFFIX[op]}"] = _coerce_filter_value(f, value)
    if for_count:
        return params
    order = _order_param(entity, parsed.sort)
    if order:
        params["sort"] = order
    params["page"] = str(parsed.page)
    params["pageSize"] = str(parsed.page_size)
    # NOTE: no ``additionalProperties`` on a list. weclapp 400s when asked for many
    # (or non-requestable) nested properties at once, and a grid does not need the
    # collections/embeds — they are requested on the single-record read instead.
    return params


def _coerce_filter_value(f: Field, value: str) -> str:
    """weclapp wants boolean filter values as the strings ``true``/``false``."""
    if f.type == "boolean":
        return "true" if str(value).strip().lower() in ("true", "1", "yes") else "false"
    return value


def _order_param(entity: Entity, sort: str | None) -> str | None:
    """weclapp ``sort`` value (``field`` / ``-field``) for a known sortable field,
    else None. weclapp's descending prefix ``-`` matches our own, so it passes
    through once we know the field is sortable."""
    if not sort:
        return None
    desc = sort.startswith("-")
    out_key = sort[1:] if desc else sort
    f = _query_index(entity).get(out_key)
    if f is None or not f.sortable:
        return None
    return f"-{f.wire}" if desc else f.wire


def _epoch_ms_to_iso(value: Any, *, as_datetime: bool) -> Any:
    """weclapp Unix-epoch-milliseconds -> ISO date/datetime. Non-numeric passes
    through unchanged (already a string, or null)."""
    if value is None or isinstance(value, str):
        return value
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return value
    return dt.isoformat() if as_datetime else dt.date().isoformat()


def _iso_to_epoch_ms(value: Any) -> Any:
    """The inverse of :func:`_epoch_ms_to_iso` for the write path: ISO date/datetime
    string -> weclapp Unix-epoch-milliseconds. Numbers and unparseable values pass
    through unchanged."""
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    for parse in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=UTC),
    ):
        try:
            dt = parse(text)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    return value


def write_payload(entity: Entity, model: dict[str, Any]) -> dict[str, Any]:
    """Our outward record -> a weclapp write payload. Only writable scalars and
    references are sent (read-only fields, unknown keys, collections and embeds are
    dropped): scalar out_key -> weclapp wire (epoch fields back to ms); a reference
    value ``{"id": …}`` (or a bare id) -> the weclapp ``<name>Id`` string."""
    out: dict[str, Any] = {}
    for f in entity.scalars:
        if f.writable and f.out_key() in model:
            val = model[f.out_key()]
            out[f.wire] = _iso_to_epoch_ms(val) if f.epoch else val
    for ref in entity.references:
        if ref.writable and ref.out_key() in model:
            val = model[ref.out_key()]
            out[ref.wire] = val.get("id") if isinstance(val, dict) else val
    return out


def transform_record(entity: Entity, raw: dict[str, Any]) -> dict[str, Any]:
    """One weclapp record -> our outward record. Built ONLY from declared fields
    (+ a stringified ``id``/``uuid``), so the shape is predictable and no unknown
    weclapp key leaks through."""
    if not isinstance(raw, dict):
        return {}
    rid = raw.get("id")
    out: dict[str, Any] = {}
    if rid is not None:
        out["id"] = str(rid)
        out["uuid"] = str(rid)
    for f in entity.scalars:
        if f.wire in raw:
            val = raw[f.wire]
            if f.epoch:
                val = _epoch_ms_to_iso(val, as_datetime=(f.type == "datetime"))
            out[f.out_key()] = val
    for ref in entity.references:
        if ref.wire in raw:
            fk = raw[ref.wire]
            out[ref.out_key()] = {"id": str(fk)} if fk not in (None, "") else None
    for emb in entity.embeds:
        sub = raw.get(emb.wire)
        out[emb.out_key()] = _transform_sub(emb.fields, sub) if isinstance(sub, dict) else None
    for col in entity.collections:
        items = raw.get(col.wire)
        out[col.out_key()] = (
            [_transform_sub(col.fields, it) for it in items if isinstance(it, dict)]
            if isinstance(items, list)
            else []
        )
    return out


def _transform_sub(fields: tuple[Any, ...], raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rid = raw.get("id")
    if rid is not None:
        out["id"] = str(rid)
    for f in fields:
        if isinstance(f, Reference):
            if f.wire in raw:
                fk = raw[f.wire]
                out[f.out_key()] = {"id": str(fk)} if fk not in (None, "") else None
        elif isinstance(f, Field) and f.wire in raw:
            val = raw[f.wire]
            if f.epoch:
                val = _epoch_ms_to_iso(val, as_datetime=(f.type == "datetime"))
            out[f.out_key()] = val
    return out


# ---- adapter engine ---------------------------------------------------------


class WeclappAdapterBase:
    """Live REST proxy to one weclapp entity, driven by an :class:`Entity`."""

    manifest: EmulationManifest
    entity: Entity

    def __init__(self, entity: Entity, *, connector_id: str = CORE_ID) -> None:
        self.entity = entity
        # The connector this adapter's credentials live under — weclapp_core
        # passes its own id so the cores stay fully separate.
        self.connector_id = connector_id
        self.manifest = EmulationManifest(
            key=entity.key,
            label_en=entity.label_en,
            category=entity.category,
            rollout_batch="weclapp",
            adapter=f"{CORE_ID}.{entity.key}",
            source_apis=("weclapp",),
            operations=entity.operations,
        )

    # ---- schema (static; no upstream call) ---------------------------------

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        e = self.entity
        properties: dict[str, Any] = {}
        for f in e.scalars:
            properties[f.out_key()] = self._scalar_prop(f)
        for ref in e.references:
            properties[ref.out_key()] = self._reference_prop(ref)
        for emb in e.embeds:
            properties[emb.out_key()] = {
                "type": "embedded",
                "label": emb.label,
                "section": emb.section,
                "access": "readOnly",
                "properties": self._sub_properties(emb.fields),
            }
        for col in e.collections:
            properties[col.out_key()] = {
                "type": "collection",
                "label": col.label,
                "section": col.section,
                "access": "readOnly",
                "node": {"properties": self._sub_properties(col.fields)},
            }
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

    def _scalar_prop(self, f: Field) -> dict[str, Any]:
        prop: dict[str, Any] = {
            "type": f.type,
            "label": f.label or f.wire,
            "section": f.section,
            "filterable": f.filterable,
            "sortable": f.sortable,
            "searchable": f.searchable,
            "previewable": self._previewable(f),
        }
        if f.preview >= 0:
            prop["previewOrder"] = f.preview
        if not f.writable:
            prop["access"] = "readOnly"
        if f.writable and f.required:
            prop["rules"] = ["required"]
        if f.type == "select":
            prop["options"] = [{"value": v, "label": lbl} for v, lbl in f.options]
        if f.filterable:
            prop["filterOperators"] = list(_FILTER_OPS_BY_TYPE.get(f.type, ()))
        return prop

    def _reference_prop(self, ref: Reference) -> dict[str, Any]:
        prop: dict[str, Any] = {
            "type": "reference",
            "label": ref.label,
            "section": ref.section,
            "reference": ref.reference,
            "renderProperty": ref.render_property,
            "filterable": False,
            "sortable": False,
            "searchable": False,
            "previewable": ref.preview >= 0,
        }
        if ref.preview >= 0:
            prop["previewOrder"] = ref.preview
        if not ref.writable:
            prop["access"] = "readOnly"
        return prop

    def _sub_properties(self, fields: tuple[Any, ...]) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for f in fields:
            if isinstance(f, Reference):
                props[f.out_key()] = self._reference_prop(f)
            elif isinstance(f, Field):
                props[f.out_key()] = {
                    "type": f.type,
                    "label": f.label or f.wire,
                    "section": f.section,
                    "access": "readOnly",
                    "filterable": False,
                    "sortable": False,
                    "searchable": False,
                }
        return props

    def _previewable(self, f: Field) -> bool:
        if f.preview >= 0:
            return True
        return not self.entity.has_preview_fields and f.out_key() == self.entity.label_field

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
        client: Any | None = None,
    ) -> AdapterResponse:
        del base_url, token, accept_language  # weclapp has its own host + auth.
        method_u = method.upper()
        ops = self.entity.operations
        wc = WeclappClient(client, connector_id=self.connector_id)
        try:
            if method_u == "GET":
                return await (self._read(wc, handle) if handle else self._list(wc, query))
            if method_u == "POST" and handle is None and "create" in ops:
                return await self._create(wc, body)
            if method_u in ("PUT", "PATCH") and handle and "update" in ops:
                return await self._update(wc, handle, body)
            if method_u == "DELETE" and handle and "delete" in ops:
                return await self._delete(wc, handle)
            return self._status(405, "Operation not permitted on this entity")
        except CoreCredentialsMissing as exc:
            return self._credentials_missing_response(exc)
        except httpx.HTTPError as exc:
            return self._error(exc)

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
        return self._status(405, "weclapp core exposes no actions yet")

    async def _list(self, wc: WeclappClient, query: list[tuple[str, str]]) -> AdapterResponse:
        e = self.entity
        parsed = parse_query(query)
        total = await wc.count(e.endpoint, params=build_list_params(e, parsed, for_count=True))
        raw = await wc.list(e.endpoint, params=build_list_params(e, parsed))
        rows = raw if isinstance(raw, list) else []
        records = [transform_record(e, r) for r in rows]
        # Both the list view and the entity-tile count badge read ``meta.total``;
        # ``extra.total`` is the documented gateway envelope. Emit both.
        body = json.dumps(
            {
                "data": records,
                "meta": {"total": total, "count": len(records)},
                "extra": {"total": total},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return AdapterResponse(200, body, {"content-type": "application/json"})

    async def _read(self, wc: WeclappClient, handle: str) -> AdapterResponse:
        e = self.entity
        # The single-record read is where we DO want the nested collections/embeds,
        # so request them via ``additionalProperties`` — but weclapp 400s if any name
        # in the set isn't requestable, so fall back to a plain read on a 400.
        params = (
            {"additionalProperties": ",".join(e.additional_properties)}
            if e.additional_properties
            else None
        )
        try:
            raw = await wc.get(e.endpoint, handle, params=params)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code == 404:
                return self._status(404, f"Not found: {handle}")
            if code == 400 and params:
                raw = await wc.get(e.endpoint, handle)  # retry without additionalProperties
            else:
                raise
        if not isinstance(raw, dict) or raw.get("id") is None:
            return self._status(404, f"Not found: {handle}")
        body = json.dumps({"data": transform_record(e, raw)}, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(200, body, {"content-type": "application/json"})

    # ---- writes (only on an entity whose ``operations`` opt in) --------------

    @staticmethod
    def _parse_body(body: bytes | None) -> tuple[dict[str, Any] | None, AdapterResponse | None]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, WeclappAdapterBase._status(400, f"Invalid JSON body: {exc}")
        if not isinstance(payload, dict):
            return None, WeclappAdapterBase._status(400, "Body must be a JSON object")
        return payload, None

    async def _create(self, wc: WeclappClient, body: bytes | None) -> AdapterResponse:
        e = self.entity
        payload, error = self._parse_body(body)
        if error is not None:
            return error
        wire = write_payload(e, payload)
        if not wire:
            return self._status(400, "No writable fields supplied")
        created = await wc.create(e.endpoint, wire)
        rec = transform_record(e, created) if isinstance(created, dict) else {}
        data = json.dumps({"data": rec}, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(201, data, {"content-type": "application/json"})

    async def _update(self, wc: WeclappClient, handle: str, body: bytes | None) -> AdapterResponse:
        e = self.entity
        payload, error = self._parse_body(body)
        if error is not None:
            return error
        changes = write_payload(e, payload)
        if not changes:
            return self._status(400, "No writable fields supplied")
        # weclapp PUT is a full-object replacement with optimistic locking (version),
        # so read-modify-write: fetch current, overlay the changes, put it back.
        try:
            current = await wc.get(e.endpoint, handle)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return self._status(404, f"Not found: {handle}")
            raise
        if not isinstance(current, dict) or current.get("id") is None:
            return self._status(404, f"Not found: {handle}")
        updated = await wc.update(e.endpoint, handle, {**current, **changes})
        rec = transform_record(e, updated) if isinstance(updated, dict) else {}
        data = json.dumps({"data": rec}, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(200, data, {"content-type": "application/json"})

    async def _delete(self, wc: WeclappClient, handle: str) -> AdapterResponse:
        e = self.entity
        try:
            await wc.delete(e.endpoint, handle)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return self._status(404, f"Not found: {handle}")
            raise
        data = json.dumps({"data": {"id": handle}}, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(200, data, {"content-type": "application/json"})

    # ---- responses ---------------------------------------------------------

    def _credentials_missing_response(self, exc: CoreCredentialsMissing) -> AdapterResponse:
        body = json.dumps(
            {"title": "weclapp connection is not configured.", **credentials_error_payload(exc)},
            ensure_ascii=False,
        ).encode("utf-8")
        # 424 Failed Dependency: a config gap, not a backend error.
        return AdapterResponse(424, body, {"content-type": "application/json"})

    def _error(self, exc: Exception) -> AdapterResponse:
        logger.warning("weclapp: request failed for %s: %s", self.entity.key, exc)
        body = json.dumps({"title": f"weclapp backend error: {exc}"}).encode("utf-8")
        return AdapterResponse(502, body, {"content-type": "application/json"})

    @staticmethod
    def _status(code: int, message: str) -> AdapterResponse:
        body = json.dumps({"title": message}).encode("utf-8")
        return AdapterResponse(code, body, {"content-type": "application/json"})


__all__ = [
    "CORE_ID",
    "WECLAPP_FIELDS",
    "WeclappClient",
    "Field",
    "Reference",
    "Embed",
    "Collection",
    "Entity",
    "ParsedQuery",
    "parse_query",
    "build_list_params",
    "transform_record",
    "write_payload",
    "WeclappAdapterBase",
]
