"""Odoo core — the entity roster, resolved live from the Odoo instance.

Nothing about the roster is modeled by hand. Odoo registers every model in
``ir.model`` and describes every field via ``fields_get``, uniformly across
versions, modules and per-customer customizations — so the entity set, the field
contract and the writability flags are all derived at request time from the
tenant's own instance:

- **Roster**: ``ir.model`` ``search_read`` (non-transient, non-abstract, minus a
  small framework-namespace blocklist — ``ir.*``, ``bus.*``, … are ORM plumbing,
  not business data). A custom module's ``x_``/own models surface automatically.
- **Fields**: ``fields_get`` per model. Scalars map through ``_TYPE_MAP``;
  ``many2one`` becomes a reference when its target model is in the roster;
  ``one2many``/``many2many`` become collections (children resolved on the
  single-record read only — never fanned out across a list page).
- **Writes**: a field is writable when Odoo says it isn't ``readonly`` (system
  bookkeeping fields excepted); ``create``/``update`` are offered per model
  according to ``check_access_rights`` and enforced again by Odoo itself on
  every ``create``/``write`` call.

Adapters are built per request via the core's ``adapters_factory`` (behind a
short TTL cache), exactly like the Phoenix core — the catalogue always shows
what *this tenant's* Odoo currently serves, and degrades to an empty roster when
no connection is configured.

To inspect the raw roster against your own instance:
  curl -s $ODOO_URL/jsonrpc -d '{"jsonrpc":"2.0","method":"call","params":
  {"service":"object","method":"execute_kw","args":[DB,UID,KEY,"ir.model",
  "search_read",[[["transient","=",false]]],{"fields":["model","name"]}]},"id":1}'
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from entity_registry.core_sdk import EmulationManifest

from entity_registry.core_sdk import CoreCredentialsMissing
from .base import (
    _METADATA_TTL_SECONDS,
    _QUERYABLE_TYPES,
    _TYPE_MAP,
    Collection,
    Entity,
    Field,
    OdooAdapterBase,
    OdooRPCError,
    _config,
    _existing,
    _fields_get,
    _model_types,
    _rpc_sync,
    _transform,
    _uid_sync,
)

logger = logging.getLogger(__name__)

_ROSTER_TTL_SECONDS = 600.0
_ACCESS_TTL_SECONDS = 300.0
# Ceiling for children resolved per collection on a single-record read. A
# partner can point at thousands of moves; the record view needs a sample, not
# an export.
_COLLECTION_LIMIT = 100

# Framework namespaces that are ORM/UI plumbing, not business data. This is a
# property of the Odoo framework itself (stable across instances), not
# per-customer curation.
_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "ir.",
    "base.",
    "base_",
    "bus.",
    "web.",
    "web_",
    "report.",
    "auth_",
    "iap.",
    "digest.",
    "onboarding.",
    "html_editor.",
    "spreadsheet.",
    "studio.",
)
_EXCLUDED_MODELS = frozenset({"base", "_unknown"})

# Model-namespace prefix (first dotted segment) -> catalogue category.
_CATEGORY_BY_PREFIX: dict[str, str] = {
    "res": "masterdata",
    "product": "masterdata",
    "uom": "masterdata",
    "sale": "sales",
    "crm": "sales",
    "loyalty": "sales",
    "utm": "sales",
    "purchase": "purchasing",
    "stock": "warehousing",
    "delivery": "warehousing",
    "account": "accounting",
    "payment": "accounting",
    "mrp": "manufacturing",
    "quality": "manufacturing",
    "maintenance": "manufacturing",
    "repair": "manufacturing",
    "hr": "hr",
    "resource": "hr",
    "project": "projects",
    "calendar": "projects",
    "mail": "communication",
    "discuss": "communication",
    "sms": "communication",
}

# System bookkeeping fields Odoo may report as writable but that must never be
# set through this surface.
_PROTECTED_FIELDS = frozenset(
    {"id", "display_name", "create_date", "create_uid", "write_date", "write_uid", "__last_update"}
)
# mail.thread / mail.activity mixin chatter — present on most business models,
# framework noise as record fields.
_CHATTER_FIELDS = frozenset(
    {
        "message_ids",
        "message_follower_ids",
        "message_partner_ids",
        "website_message_ids",
        "activity_ids",
        "rating_ids",
    }
)


# ---- roster -------------------------------------------------------------------

# (base_url, db) -> (fetched_at, roster). The roster is the model index only —
# (model, label, category) from ir.model. Keyed by instance so one tenant's
# catalogue is never served to another.
_ROSTER_CACHE: dict[tuple[str, str], tuple[float, tuple[tuple[str, str, str], ...]]] = {}
_ROSTER_LOCK = threading.Lock()


def _category(model: str) -> str:
    return _CATEGORY_BY_PREFIX.get(model.split(".", 1)[0], "other")


def _excluded(model: str) -> bool:
    return model in _EXCLUDED_MODELS or model.startswith(_EXCLUDED_PREFIXES)


def fetch_odoo_roster() -> tuple[tuple[str, str, str], ...]:
    """The tenant's live model roster from ``ir.model``: ``(model, label,
    category)`` per entity. Degrades to an empty tuple when no Odoo connection
    is configured, and to the last good roster (stale beats broken) on a
    transient fetch failure — it never raises, so it is safe on the synchronous
    composition path."""
    try:
        base, db, login, secret = _config()
    except CoreCredentialsMissing:
        return ()
    now = time.monotonic()
    cache_key = (base, db)
    with _ROSTER_LOCK:
        cached = _ROSTER_CACHE.get(cache_key)
        if cached and now - cached[0] < _ROSTER_TTL_SECONDS:
            return cached[1]
    try:
        uid = _uid_sync(base, db, login, secret)
        # ``abstract`` exists on ir.model only in newer Odoo versions — request
        # it when the instance knows it, so mixins drop out where possible.
        wanted = ["model", "name"]
        if "abstract" in _fields_get("ir.model"):
            wanted.append("abstract")
        rows = _rpc_sync(
            base,
            "object",
            "execute_kw",
            [
                db,
                uid,
                secret,
                "ir.model",
                "search_read",
                [[["transient", "=", False]]],
                {"fields": wanted, "order": "model asc"},
            ],
        )
    except (httpx.HTTPError, OdooRPCError, CoreCredentialsMissing) as exc:
        logger.warning("odoo: roster fetch failed: %s", exc)
        with _ROSTER_LOCK:
            cached = _ROSTER_CACHE.get(cache_key)
        return cached[1] if cached else ()
    roster = tuple(
        (model, str(row.get("name") or model), _category(model))
        for row in (rows or [])
        if isinstance(row, dict)
        and (model := str(row.get("model") or ""))
        and not _excluded(model)
        and not row.get("abstract")
    )
    with _ROSTER_LOCK:
        _ROSTER_CACHE[cache_key] = (now, roster)
    return roster


# ---- access rights --------------------------------------------------------------

# (base, db, model, operation) -> (fetched_at, allowed)
_ACCESS_CACHE: dict[tuple[str, str, str, str], tuple[float, bool]] = {}
_ACCESS_LOCK = threading.Lock()


def _has_access(model: str, operation: str) -> bool:
    """Whether the connected Odoo user may perform ``operation`` on ``model``,
    per ``check_access_rights``. Permissive on failure (older/newer signature,
    transport error): Odoo enforces its ACLs on every actual write anyway — this
    flag only tunes what the UI offers."""
    try:
        base, db, login, secret = _config()
    except CoreCredentialsMissing:
        return False
    cache_key = (base, db, model, operation)
    now = time.monotonic()
    with _ACCESS_LOCK:
        cached = _ACCESS_CACHE.get(cache_key)
        if cached and now - cached[0] < _ACCESS_TTL_SECONDS:
            return cached[1]
    try:
        uid = _uid_sync(base, db, login, secret)
        allowed = bool(
            _rpc_sync(
                base,
                "object",
                "execute_kw",
                [
                    db,
                    uid,
                    secret,
                    model,
                    "check_access_rights",
                    [operation],
                    {"raise_exception": False},
                ],
            )
        )
    except (httpx.HTTPError, OdooRPCError) as exc:
        logger.info(
            "odoo: access check failed for %s/%s (assuming allowed): %s", model, operation, exc
        )
        allowed = True
    with _ACCESS_LOCK:
        _ACCESS_CACHE[cache_key] = (now, allowed)
    return allowed


# ---- default list columns (Odoo list view) ---------------------------------------

# (base, db, model) -> (fetched_at, field names in view order)
_VIEW_CACHE: dict[tuple[str, str, str], tuple[float, tuple[str, ...]]] = {}
_VIEW_LOCK = threading.Lock()
# Fallback when no list view is readable: the first stored business scalars in
# schema order still fill a useful table.
_FALLBACK_COLUMNS = 8


def _parse_view_arch(arch: str) -> tuple[str, ...]:
    """Field names of a list-view ``arch``, in view order — skipping columns the
    view itself hides (``invisible``/``column_invisible``/``optional=hide``)."""
    # The arch comes from the tenant's own authenticated Odoo, but harden
    # anyway: reject DTD/entity constructs outright (the vectors S314 warns
    # about), then parse with stdlib ElementTree.
    if "<!DOCTYPE" in arch or "<!ENTITY" in arch:
        return ()
    try:
        root = ET.fromstring(arch)  # noqa: S314 — DTD/entities rejected above
    except ET.ParseError:
        return ()
    names: list[str] = []
    for el in root.iter("field"):
        name = el.get("name")
        if not name or name in names:
            continue
        if el.get("column_invisible") in ("1", "True", "true"):
            continue
        if el.get("invisible") in ("1", "True", "true"):
            continue
        if el.get("optional") == "hide":
            continue
        names.append(name)
    return tuple(names)


def _list_view_fields(model: str) -> tuple[str, ...]:
    """The columns of the model's default Odoo list view, in order.

    This is the instance's own curation of "what belongs in a list" — the same
    columns the customer sees in Odoo, including their customizations — so it
    drives our default table columns. Tries ``get_view`` with ``list`` (Odoo
    >= 18) then ``tree`` (16/17), then legacy ``fields_view_get``; an unreadable
    view yields ``()`` and the caller falls back to a schema-order heuristic.
    TTL-cached per instance + model; never raises."""
    try:
        base, db, login, secret = _config()
    except CoreCredentialsMissing:
        return ()
    cache_key = (base, db, model)
    now = time.monotonic()
    with _VIEW_LOCK:
        cached = _VIEW_CACHE.get(cache_key)
        if cached and now - cached[0] < _METADATA_TTL_SECONDS:
            return cached[1]
    arch = ""
    try:
        uid = _uid_sync(base, db, login, secret)
        for method, kwargs in (
            ("get_view", {"view_type": "list"}),
            ("get_view", {"view_type": "tree"}),
            ("fields_view_get", {"view_type": "tree"}),
        ):
            try:
                view = _rpc_sync(
                    base, "object", "execute_kw", [db, uid, secret, model, method, [], kwargs]
                )
            except OdooRPCError:
                continue
            if isinstance(view, dict) and view.get("arch"):
                arch = str(view["arch"])
                break
    except (httpx.HTTPError, OdooRPCError, CoreCredentialsMissing) as exc:
        logger.info("odoo: list view fetch failed for %s: %s", model, exc)
        return ()
    fields = _parse_view_arch(arch)
    with _VIEW_LOCK:
        _VIEW_CACHE[cache_key] = (now, fields)
    return fields


def _preview_order(model: str, main: dict) -> dict[str, int]:
    """``{odoo field name: default column position}`` for the entity. The Odoo
    list view wins; without one, the first stored business scalars in schema
    order keep the table usable."""
    # Non-stored computed fields in a list view are usually UI sugar (activity
    # decorations, JSON popovers), not business columns — skip them, except the
    # name-ish fields, which are legitimately computed.
    view_names = [
        name
        for name in _list_view_fields(model)
        if isinstance(main.get(name), dict)
        and (main[name].get("store") or name in ("display_name", "name"))
    ]
    order = {name: idx for idx, name in enumerate(view_names)}
    if order:
        return order
    picked = [
        name
        for name, meta in main.items()
        if isinstance(meta, dict)
        and meta.get("type") in _TYPE_MAP
        and meta.get("store")
        and name not in _PROTECTED_FIELDS
        and name not in _CHATTER_FIELDS
    ][:_FALLBACK_COLUMNS]
    return {name: idx for idx, name in enumerate(picked)}


# ---- entity synthesis -----------------------------------------------------------


def _scalar_field(name: str, meta: dict, roster_models: frozenset[str], preview: int = -1) -> Field:
    ftype = meta.get("type")
    ref = ""
    if ftype == "many2one":
        target = str(meta.get("relation") or "")
        if target in roster_models:
            ref = target
    # A many2one without a roster target renders as its display name — writing
    # it would forward that name string as the value, so it stays read-only.
    writable = (
        not meta.get("readonly")
        and name not in _PROTECTED_FIELDS
        and (ftype != "many2one" or bool(ref))
    )
    return Field(
        name,
        label=str(meta.get("string") or name),
        writable=writable,
        required=writable and bool(meta.get("required")),
        ref=ref,
        preview=preview,
    )


def _child_fields(child_model: str, roster_models: frozenset[str]) -> tuple[Field, ...]:
    """Scalar fields of a collection's child model (one level deep — children
    carry no collections of their own)."""
    fields = _fields_get(child_model)
    return tuple(
        _scalar_field(name, meta, roster_models)
        for name, meta in fields.items()
        if isinstance(meta, dict)
        and meta.get("type") in _TYPE_MAP
        and name not in _PROTECTED_FIELDS
        and name not in _CHATTER_FIELDS
    )


def _synthesize_entity(
    model: str, label: str, category: str, roster_models: frozenset[str]
) -> Entity:
    """Build the full :class:`Entity` declaration from the model's live
    ``fields_get`` contract. Raises (creds / transport / RPC) when the schema
    can't be fetched cold — callers on the request path already handle those."""
    main = _fields_get(model)
    preview = _preview_order(model, main)
    scalars: list[Field] = []
    collections: list[Collection] = []
    for name, meta in main.items():
        if not isinstance(meta, dict) or name in _CHATTER_FIELDS:
            continue
        ftype = meta.get("type")
        if ftype in _TYPE_MAP:
            scalars.append(_scalar_field(name, meta, roster_models, preview.get(name, -1)))
        elif ftype in ("one2many", "many2many"):
            child = str(meta.get("relation") or "")
            if child not in roster_models:
                continue
            if ftype == "one2many":
                collections.append(
                    Collection(
                        key=name,
                        label=str(meta.get("string") or name),
                        source_field=name,
                        child_model=child,
                        fields=_child_fields(child, roster_models),
                        writable=not meta.get("readonly"),
                    )
                )
            else:
                # many2many: a read-only name list — enough to see the links
                # without pulling the full child schema into the node.
                collections.append(
                    Collection(
                        key=name,
                        label=str(meta.get("string") or name),
                        source_field=name,
                        child_model=child,
                        fields=(Field("display_name", label="Name"),),
                    )
                )
    label_field = "display_name" if "display_name" in main else "id"
    return Entity(
        key=model,
        label_en=label,
        category=category,
        model=model,
        label_field=label_field,
        sections=(("general", "General"),),
        scalars=tuple(scalars),
        collections=tuple(collections),
        operations=("list", "read", "create", "update"),
    )


class OdooDynamicAdapter(OdooAdapterBase):
    """One live Odoo model, schema and writability derived per tenant at
    request time."""

    def __init__(self, model: str, label: str, category: str, roster_models: frozenset[str]):
        self._model = model
        self._label = label
        self._category = category
        self._roster_models = roster_models
        self._entity: Entity | None = None
        self.manifest = EmulationManifest(
            key=model,
            label_en=label,
            category=category,
            rollout_batch="odoo",
            adapter=f"odoo.{model}",
            source_apis=("odoo",),
            operations=("list", "read", "create", "update"),
        )

    @property
    def entity(self) -> Entity:
        """Synthesized lazily from the live schema (TTL-cached in
        ``_fields_get``). On a cold fetch failure this returns a minimal
        field-less declaration and does not cache it: the caller's own
        ``_fields_get`` hits the same failure and reports it through the shared
        credentials-missing / unreachable paths."""
        if self._entity is None:
            try:
                self._entity = _synthesize_entity(
                    self._model, self._label, self._category, self._roster_models
                )
            except (CoreCredentialsMissing, httpx.HTTPError, OdooRPCError):
                return Entity(
                    key=self._model,
                    label_en=self._label,
                    category=self._category,
                    model=self._model,
                    label_field="display_name",
                    sections=(("general", "General"),),
                    scalars=(),
                )
        return self._entity

    @entity.setter
    def entity(self, value: Entity) -> None:  # pragma: no cover - base compat
        self._entity = value

    # Write support is per-model access, not a static declaration; Odoo
    # re-enforces its ACLs on the actual create/write call.
    def _supports_create(self) -> bool:
        return _has_access(self._model, "create")

    def _supports_update(self) -> bool:
        return _has_access(self._model, "write")

    def metadata(self, accept_language: str | None = None) -> dict:
        meta = super().metadata(accept_language)
        if "error" not in meta:
            ops = ["list", "read"]
            if self._supports_create():
                ops.append("create")
            if self._supports_update():
                ops.append("update")
            meta["operations"] = ops
        return meta

    def _order(self, sort: str | None) -> str | None:
        """Additionally require a stored, queryable column — the dynamic scalar
        set includes computed non-stored fields, and ordering on one raises
        upstream."""
        order = super()._order(sort)
        if not order:
            return None
        field_name = order.split(" ", 1)[0]
        meta = _fields_get(self.entity.model).get(field_name) or {}
        if not meta.get("store") or meta.get("type") not in _QUERYABLE_TYPES:
            return None
        return order

    async def _resolve_collection(self, conn, raw, col):  # type: ignore[override]
        """Collections resolve on the single-record read only, capped at
        ``_COLLECTION_LIMIT`` children. A dynamic entity can carry many
        relational fields pointing at huge child sets — fanning those out across
        a 50-row list page would multiply round-trips and payload without
        bound."""
        if len(raw) != 1:
            return {}
        ids = list(raw[0].get(col.source_field) or [])[:_COLLECTION_LIMIT]
        if not ids:
            return {}
        fields = _existing(col.child_model, [f.odoo for f in col.fields])
        rows = await self._execute(conn, col.child_model, "read", [ids], {"fields": fields})
        types = _model_types(col.child_model)
        return {row["id"]: _transform(row, col.fields, types) for row in (rows or [])}


def build_adapters() -> tuple[OdooDynamicAdapter, ...]:
    """The Odoo adapters for the current tenant, built from the live ``ir.model``
    roster. Empty when the tenant has no Odoo connection configured. Wired as
    the core's ``adapters_factory`` so it is re-resolved per request (behind the
    TTL cache in :func:`fetch_odoo_roster`), never at import time."""
    roster = fetch_odoo_roster()
    models = frozenset(model for model, _, _ in roster)
    return tuple(
        OdooDynamicAdapter(model, label, category, models) for model, label, category in roster
    )
