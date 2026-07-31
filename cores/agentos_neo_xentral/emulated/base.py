"""Xentral V3 facade base — Mode C (see docs/guides/building-an-erp-core.md).

Outward this core speaks the redesigned model (docs/01-model.md); inward it
proxies today's Xentral API (v3 + v1/v2) and maps 1:1 — **no own persistence**
(ADR-014). Each adapter declares its outward ``fields()`` and implements
``map_read(v3_record)`` to translate a live upstream record into the new model.
This base owns the shared machinery: the metadata envelope, the HTTP GET to the
upstream (always requesting *all* statuses — ADR-007, no hidden draft filter),
and the new-model helper types (reference objects, money, quantity, ids).

Write paths are deliberately thin: fields the upstream cannot set are
``creatable/updatable = false`` and surface as blue wishes; a write that includes
them answers 409 with the field list (ADR-014). The write-orchestrator lands per
entity as we build.
"""

from __future__ import annotations

import functools
import json
import os
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

# Shared consolidated-`search` machinery (OR fan-out over per-field contains
# filters, verified live against the same v3 endpoints) — the xentral_api core
# owns the implementation; reusing it keeps ONE search behavior across cores
# (weclapp_core → agentos_neo_weclapp precedent for cross-core imports).
from xentral_entity_cores.xentral_api.emulated._search import extract_search, fan_out_search

_TIMEOUT = 60.0
_UA = "xentral-ai-agent"
# A speaking id — ``<prefix>_<numeric>`` (eid(); e.g. ``cus_20423``, ``prd_61617``).
# Reference filter values arrive in this shape but upstream filters on the bare
# numeric id, so the prefix is stripped for reference-typed filter keys.
_SPEAKING_ID = re.compile(r"^[a-z]+_[0-9]+$")


@functools.lru_cache(maxsize=1)
def _priorities() -> dict[str, Any]:
    """Per-entity blue wishes from the core's priorities.json (the living
    backlog; docs/03-mapping-layer.md §5). Missing file → empty."""
    path = os.path.join(os.path.dirname(__file__), "..", "priorities.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("entities") or {}
    except (FileNotFoundError, ValueError):
        return {}


@functools.lru_cache(maxsize=1)
def _verified() -> dict[str, Any]:
    """Live-test results per entity (verified.json) — the GREEN/RED side of the
    living backlog. ``entities.<Key>.fields.<path>`` → per-facet pass/fail. Written
    by checks/verify.py; missing file → empty (all untested / grey)."""
    path = os.path.join(os.path.dirname(__file__), "..", "verified.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("entities") or {}
    except (FileNotFoundError, ValueError):
        return {}


@functools.lru_cache(maxsize=1)
def _descriptions() -> dict[str, Any]:
    """Human field descriptions (English) from descriptions.json. Shape:
    ``{"_shared": {<path>: text}, "<Entity>": {<path>: text}}`` — ``_shared``
    applies to every entity (the partner address/contact leaves live there), an
    entity-specific entry wins. Stamped onto the schema so the field grid can
    explain non-obvious fields (billTo, parent, isDefault …). Missing file → empty."""
    path = os.path.join(os.path.dirname(__file__), "..", "descriptions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, ValueError):
        return {}


def prop(type_: str, label: str, **flags: Any) -> dict[str, Any]:
    """A schema property in the shared shape (embedded → ``properties``,
    collection → ``node.properties``); flags mirror the other cores:
    creatable / updatable / filterable / sortable / searchable / access /
    section / options / reference / renderProperty / priority."""
    return {"type": type_, "label": label, **flags}


def eid(prefix: str, numeric: Any) -> str | None:
    """Deterministic speaking id: ``<prefix><numeric>`` (ADR-002; reversible by
    stripping the prefix — no lookup needed). Cosmetic base36 encoding is a later
    refinement; determinism and reversibility are what the contract requires."""
    if numeric in (None, ""):
        return None
    return f"{prefix}{numeric}"


def ref(
    prefix: str, numeric: Any, number: Any, name: Any, collection: str
) -> dict[str, Any] | None:
    """A reference object ``{id, number, name, href}`` (ADR-001). Never a bare id."""
    ident = eid(prefix, numeric)
    if ident is None:
        return None
    out: dict[str, Any] = {"id": ident}
    if number not in (None, ""):
        out["number"] = str(number)
    if name not in (None, ""):
        out["name"] = str(name)
    out["href"] = f"/v1/{collection}/{ident}"
    return out


def money(amount: Any, currency: str = "EUR") -> dict[str, Any] | None:
    """Money as a normalized decimal STRING (ADR-006), never a float. Upstream
    hands us ``"8.00000000"``; emit ``"8.00"`` (strip trailing zeros, keep >= 2
    decimals; preserve genuine sub-cent precision like ``"1.2345"``)."""
    if amount in (None, ""):
        return None
    try:
        s = format(Decimal(str(amount)), "f")
    except (InvalidOperation, ValueError):
        return {"amount": str(amount), "currency": currency}
    if "." in s:
        intp, _, frac = s.partition(".")
        frac = frac.rstrip("0")
        frac = frac.ljust(2, "0")
        s = f"{intp}.{frac}"
    else:
        s = f"{s}.00"
    return {"amount": s, "currency": currency}


def qty(value: Any, unit: str = "piece") -> dict[str, Any]:
    """Quantity as {value, unit} (ADR-006); unit is mandatory."""
    return {"value": value, "unit": unit}


def line_qty(item: dict[str, Any]) -> Any:
    """Quantity value from a write payload's line item, tolerating both the
    canonical ``{"value": …}`` object AND a bare scalar (agents/clients send
    either). None when unset. Prevents ``'int' object has no attribute 'get'``."""
    q = item.get("quantity")
    return q.get("value") if isinstance(q, dict) else q


def line_price_net(item: dict[str, Any], currency: str = "EUR") -> dict[str, Any] | None:
    """v3 net-price payload from a line item's ``unitPrice``, tolerating both the
    canonical ``{"amount": …, "currency": …}`` object AND a bare scalar amount.
    None when there is no amount to send."""
    up = item.get("unitPrice")
    if isinstance(up, dict):
        amount, cur = up.get("amount"), up.get("currency")
    else:
        amount, cur = up, None
    if amount is None:
        return None
    return {"net": {"amount": str(amount), "currency": cur or currency}}


def purchase_price_prop(*, updatable: bool = False) -> dict[str, Any]:
    """A line item's ``purchasePrice`` (EK) — the cost price carried on the position
    itself, not the supplier price list (that is the PurchasePrice entity). Writable
    upstream on offer, salesOrder, invoice and creditNote positions; proforma has no
    EK columns. Setting it marks the price as manually provided (upstream clears the
    price-list link) and feeds the contribution margin.

    ``updatable`` follows what the entity actually does on UPDATE: salesOrder
    reconciles its positions against the v3 lineItems sub-resource, the other three
    reject ``items`` outright, so there the EK is create-only."""
    flags: dict[str, Any] = {"creatable": True}
    if updatable:
        flags["updatable"] = True
    return prop(
        "embedded",
        "Purchase price",
        **flags,
        properties={
            "amount": prop("decimal", "Amount"),
            "currency": prop("string", "Currency"),
        },
    )


def line_purchase_price_net(item: dict[str, Any], currency: str = "EUR") -> dict[str, Any] | None:
    """v3 ``purchasePrice`` payload from a line item's ``purchasePrice``, tolerating
    both the canonical ``{"amount": …, "currency": …}`` object AND a bare scalar
    amount. None when there is no amount to send — upstream rejects an explicit null
    (its EK columns are NOT NULL), so a cleared EK is never emitted."""
    pp = item.get("purchasePrice")
    if isinstance(pp, dict):
        amount, cur = pp.get("amount"), pp.get("currency")
    else:
        amount, cur = pp, None
    if amount is None:
        return None
    return {"net": {"amount": str(amount), "currency": cur or currency}}


def map_purchase_price(li: dict[str, Any], currency: str = "EUR") -> dict[str, Any] | None:
    """Read a v3 line item's ``purchasePrice.net`` back into the model's flat money
    shape. None when upstream reports no EK."""
    net = (li.get("purchasePrice") or {}).get("net") or {}
    return money(net.get("amount"), net.get("currency") or currency)


def rejected_item_keys(items: Any, item_props: dict[str, Any]) -> set[str]:
    """``items.<key>`` for every line-item key the entity does not model.

    A line item's ``_item_to_v3`` picks the keys it knows and ignores the rest, so
    an unsupported one used to vanish without a trace — a create carrying it came
    back 201 with the value never written. Top-level keys have always surfaced as
    a wish via ``rejected``; this puts item sub-keys on the same footing.

    The schema is the allowlist, so read-only leaves (``totals``, ``fulfillment``,
    ``id`` …) pass silently and a read-modify-write round-trip stays quiet — only
    keys the entity does not declare at all are reported."""
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for it in items:
        if isinstance(it, dict):
            out |= {f"items.{k}" for k in it if k not in item_props}
    return out


def tags_prop(*, writable: bool = False) -> dict[str, Any]:
    """The model's ``tags`` field: a plain string array of tag titles
    (docs/01-model.md §6.1 ``"tags": ["b2b", "vip"]``). Declared as the FE's
    ``tag`` type — that is the contract for the pill renderer AND for the table
    column picker (``collection`` is excluded from columns). v3 accepts titles on
    write, so the string form round-trips; color/group live on the Tag entity."""
    flags: dict[str, Any] = {"filterable": True}
    if writable:
        flags["creatable"] = True
        flags["updatable"] = True
    else:
        flags["access"] = "readOnly"
    return {"type": "tag", "label": "Tags", "section": "general", **flags}


def map_tags(raw: Any) -> list[str]:
    """v3 ``tags`` include → the model's title array. Documents deliver
    ``[{id, title}]``, products deliver ``[{id, name, color}]`` — accept both."""
    out: list[str] = []
    for t in raw or []:
        if isinstance(t, dict) and (t.get("title") or t.get("name")):
            out.append(str(t.get("title") or t.get("name")))
        elif isinstance(t, str) and t:
            out.append(t)
    return out


def tags_to_v3(value: Any) -> list[dict[str, str]]:
    """Model tags (title array, or [{title}] objects) → the v3 write shape
    ``[{"title": …}]``. NOTE: a v3 update REPLACES the whole tag list — the
    client sends the full desired set (the model's PATCH semantics)."""
    out: list[dict[str, str]] = []
    for t in value or []:
        title = t.get("title") if isinstance(t, dict) else t
        if title:
            out.append({"title": str(title)})
    return out


class FacadeAdapterBase:
    """Concrete adapters set ``manifest``, ``v3_path`` (upstream collection),
    ``sections``, ``preview_template``, ``include`` (upstream ?include=), and
    implement ``fields()`` + ``map_read()`` (+ optional ``steps()`` /
    ``actions()``)."""

    manifest: EmulationManifest
    v3_path: str = ""  # e.g. "/api/v3/salesOrders"
    # Upstream collection for WRITES when it differs from the read path. Most
    # entities read and write the same v3 collection (write_path stays ""); a few
    # read a purpose-built v3 read model but must write through an older
    # generation (Product: reads /api/v3/products, writes /api/v2/products).
    # ``_send`` (POST/PATCH/PUT/DELETE) targets ``write_path or v3_path``; reads
    # (_get) always use v3_path.
    write_path: str = ""
    include: str = ""
    sections: dict[str, dict[str, str]] = {}
    preview_template: str = "{{number}}"
    # v1 list endpoints (salesPrices, shipments, storageLocations, inventoryRuns,
    # pickLists…) REQUIRE page[number] AND page[size] with size 10..50; setting
    # this flag makes ``_get`` guarantee both (clamping the requested size).
    v1_paging: bool = False
    # MODEL field path → upstream filter/sort wire key (number → documentNumber).
    query_aliases: dict[str, str] = {}
    # MODEL field path → {model enum value → upstream enum value}; applied to
    # filter values so a consumer filters by the status chain the model shows
    # (fulfilled) while the upstream receives its own vocabulary (completed).
    filter_value_maps: dict[str, dict[str, str]] = {}
    # MODEL field path → {model filter op → upstream filter op}; for upstreams
    # that reject an op on a specific property (BF rejects equals/in on tags —
    # only contains works there).
    filter_op_maps: dict[str, dict[str, str]] = {}
    # BF entity endpoints (/api/entity/…) take sort as sort[0][key]+[direction]
    # instead of the flat ``sort=-field`` the v3 REST endpoints use.
    bf_sort: bool = False
    # Upstream field appended as a deterministic secondary sort key on the flat
    # (v3) sort path. Many upstreams leave the order of rows that share the
    # primary key UNSPECIFIED — /api/v3/customers returns a non-deterministic
    # order for the ~20k rows sharing a bulk-seed createdAt (F5), so paging and
    # "oldest/newest by createdAt" are irreproducible. ``id`` is stable and
    # unique; set to None on an adapter whose upstream rejects a compound sort.
    sort_tiebreak: str | None = "id"

    # ---- schema ----------------------------------------------------------
    def fields(self) -> dict[str, dict[str, Any]]:  # pragma: no cover - overridden
        raise NotImplementedError

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def steps(self) -> list[dict[str, Any]]:
        return []

    def actions(self) -> list[dict[str, Any]]:
        return []

    def action_def(
        self,
        key: str,
        label: str,
        *,
        destructive: bool = False,
        description: str | None = None,
        wish: str | None = None,
        command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One entry of the model's action catalogue (docs/01-model.md §8).

        ``wish`` marks a planned-but-unimplemented action: it is DECLARED (the
        catalogue is the contract) but executing it returns 409 with this reason
        — the action-side twin of the blue field wishes (ADR-014).
        """
        d: dict[str, Any] = {
            "key": key,
            "label": label,
            "bulk": False,
            "method": "PATCH",
            "path": f"/api/entity/{self.manifest.key}/actions/{key}",
            "destructive": destructive,
        }
        if description:
            d["description"] = description
        if command:
            d["command"] = command
        if wish:
            d["wish"] = wish
        return d

    @staticmethod
    def step_cmd(key: str, label: str, *, wish: str | None = None) -> dict[str, Any]:
        cmd: dict[str, Any] = {"key": key, "label": label}
        if wish:
            cmd["wish"] = wish
        return cmd

    @staticmethod
    def _resolve_path(properties: dict[str, Any], path: str) -> dict[str, Any] | None:
        """Walk a dotted path into the nested schema (embedded → ``properties``,
        collection → ``node.properties``) and return the leaf spec."""
        node: Any = properties
        spec: dict[str, Any] | None = None
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            spec = node.get(part)
            if not isinstance(spec, dict):
                return None
            node = spec.get("properties") or (spec.get("node") or {}).get("properties") or {}
        return spec

    def _apply_priorities(self, properties: dict[str, Any]) -> None:
        """Stamp the hand-curated blue wishes (priorities.json) onto the schema —
        the living backlog (docs/03-mapping-layer.md §5). Renders blue only where
        the op is actually unavailable."""
        by_field: dict[str, dict[str, str]] = {}
        for entry in _priorities().get(self.manifest.key) or ():
            field = entry.get("field")
            reason = entry.get("reason") or ""
            for op in entry.get("ops") or ():
                by_field.setdefault(field, {})[op] = reason
        for field, ops in by_field.items():
            spec = self._resolve_path(properties, field) if "." in field else properties.get(field)
            if isinstance(spec, dict):
                spec["priority"] = ops

    def _apply_verified(self, properties: dict[str, Any]) -> None:
        """Stamp live-test results (verified.json) onto the schema — the green/red
        side of the grid. ``fields.<path>`` → per-facet pass/fail (+ <op>Note)."""
        fields = (_verified().get(self.manifest.key) or {}).get("fields") or {}
        for path, facets in fields.items():
            spec = self._resolve_path(properties, path) if "." in path else properties.get(path)
            if isinstance(spec, dict) and isinstance(facets, dict):
                spec["verified"] = facets

    def _apply_descriptions(self, properties: dict[str, Any]) -> None:
        """Stamp English field descriptions (descriptions.json) onto the schema. The
        ``_shared`` block applies to every entity; an entity-specific entry wins; an
        inline ``description=`` on a prop() is never overwritten."""
        data = _descriptions()
        merged: dict[str, str] = {}
        merged.update(data.get("_shared") or {})
        merged.update(data.get(self.manifest.key) or {})
        for path, text in merged.items():
            if not text:
                continue
            spec = self._resolve_path(properties, path) if "." in path else properties.get(path)
            if isinstance(spec, dict) and not spec.get("description"):
                spec["description"] = text

    def search_fields(self) -> tuple[str, ...]:
        """MODEL fields flagged ``searchable`` in the entity's schema — the
        consolidated ``search`` filter fans out over exactly these, so the
        search contract lives next to the field declarations."""
        cached = type(self).__dict__.get("_search_fields_cache")
        if cached is not None:
            return cached
        fields = tuple(
            name
            for name, spec in self.fields().items()
            if isinstance(spec, dict) and spec.get("searchable")
        )
        type(self)._search_fields_cache = fields
        return fields

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        properties = self.fields()
        self._apply_priorities(properties)
        self._apply_verified(properties)
        self._apply_descriptions(properties)
        meta: dict[str, Any] = {
            "key": self.manifest.key,
            "label": self.manifest.label(accept_language),
            "operations": list(self.manifest.operations),
            "previewTemplateString": self.preview_template,
            "sections": self.sections or {"general": {"label": "General"}},
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }
        # Entity-level "what is this / when to use it" — surfaced at the head of
        # `describe` so an agent reads the entity's role before scanning fields
        # (e.g. that scoped/tiered prices live on PriceList, not on Product).
        if getattr(self.manifest, "description", ""):
            meta["description"] = self.manifest.description
        # Advertise the consolidated `search` filter only when the schema
        # actually flags fields — consumers (record pickers, list search) key
        # their server-search affordance off this list.
        if self.search_fields():
            meta["searchFields"] = list(self.search_fields())
        actions = list(self.actions())
        # Every entity with writable tags automatically gets the addTag/removeTag
        # actions — the base implements them generically (read-modify-write on the
        # full tag list, since a v3 update replaces the whole set).
        tags_spec = properties.get("tags")
        if isinstance(tags_spec, dict) and (
            tags_spec.get("creatable") or tags_spec.get("updatable")
        ):
            base_path = f"/api/entity/{self.manifest.key}/actions"
            tag_command = {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string", "label": "Tag"}},
            }
            actions += [
                {
                    "key": "addTag",
                    "label": "Add tag",
                    "bulk": False,
                    "method": "PATCH",
                    "path": f"{base_path}/addTag",
                    "destructive": False,
                    "description": "Add a tag to this record (created automatically if new).",
                    "command": tag_command,
                },
                {
                    "key": "removeTag",
                    "label": "Remove tag",
                    "bulk": False,
                    "method": "PATCH",
                    "path": f"{base_path}/removeTag",
                    "destructive": False,
                    "description": "Remove a tag from this record.",
                    "command": tag_command,
                },
            ]
        if actions:
            meta["actions"] = actions
        steps = self.steps()
        if steps:
            meta["processSteps"] = steps
        return meta

    # ---- data ------------------------------------------------------------
    @staticmethod
    def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> AdapterResponse:
        return AdapterResponse(
            status,
            json.dumps(payload).encode("utf-8"),
            headers or {"content-type": "application/json"},
        )

    def _headers(self, token: str, accept_language: str | None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "X-Pagination": "table",
        }
        if accept_language:
            h["Accept-Language"] = accept_language
        return h

    def _reference_filter_keys(self) -> set[str]:
        """Dotted model paths of every ``reference`` field, cached per adapter
        class. A reference FILTER value is a speaking id (``cus_20423``) but the
        upstream filters on the bare numeric id — ``_get`` strips the prefix for
        exactly these keys, leaving string/enum filters untouched."""
        cached = type(self).__dict__.get("_ref_filter_keys_cache")
        if cached is not None:
            return cached
        keys: set[str] = set()

        def walk(props: dict[str, Any], prefix: str = "") -> None:
            for name, spec in (props or {}).items():
                if not isinstance(spec, dict):
                    continue
                path = f"{prefix}.{name}" if prefix else name
                if spec.get("type") == "reference":
                    keys.add(path)
                sub = spec.get("properties")
                if not isinstance(sub, dict):
                    node = spec.get("node")
                    sub = node.get("properties") if isinstance(node, dict) else None
                if isinstance(sub, dict):
                    walk(sub, path)

        try:
            walk(self.fields())
        except Exception:  # noqa: BLE001 - a broken fields() must not break list
            keys = set()
        type(self)._ref_filter_keys_cache = keys
        return keys

    def _strip_reference_filter_prefixes(
        self, params: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """Reference filter values arrive as speaking ids ("cus_20423"); the
        upstream filters on the bare numeric id, so strip the prefix for
        reference-typed filter keys (mirrors ``_ref_id`` on writes). Keyed off the
        MODEL filter key, so string/enum filters (number, tags, status, …) are
        left untouched. Runs before ``query_aliases`` rewrites the ``[key]``."""
        ref_keys = self._reference_filter_keys()
        if not ref_keys:
            return params
        key_by_index = {
            k[len("filter[") : k.index("]")]: v
            for k, v in params
            if k.startswith("filter[") and k.endswith("][key]")
        }
        out: list[tuple[str, str]] = []
        for k, v in params:
            if k.startswith("filter[") and k.endswith("][value]"):
                idx = k[len("filter[") : k.index("]")]
                if (
                    key_by_index.get(idx) in ref_keys
                    and isinstance(v, str)
                    and _SPEAKING_ID.match(v)
                ):
                    v = v.split("_", 1)[1]
            out.append((k, v))
        return out

    async def _resolve_upstream_handle(
        self,
        handle: str,
        *,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> str:
        """Translate the stripped speaking id into the identifier the upstream
        GET expects. Identity by default; overridden by an adapter whose upstream
        is keyed differently from the id we expose (ChannelAdapter maps the
        numeric channel id — the form v3 document relations carry — to the BF
        salesChannel uuid, X5)."""
        return handle

    async def _get(
        self,
        base_url: str,
        token: str,
        *,
        handle: str | None,
        query: list[tuple[str, str]],
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> tuple[int, Any]:
        # Strip our speaking prefix back to the numeric upstream id for the URL.
        upstream_handle = handle
        if handle and "_" in handle:
            upstream_handle = handle.split("_", 1)[1]
        if upstream_handle is not None:
            upstream_handle = await self._resolve_upstream_handle(
                upstream_handle,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
        path = self.v3_path + (f"/{upstream_handle}" if upstream_handle else "")
        params = [(k, v) for k, v in query]
        # Translate MODEL field paths in filter keys / sort to the upstream's
        # wire keys (number → documentNumber, customer → address.id, …) and MODEL
        # enum values to upstream vocabulary (status fulfilled → completed). The
        # facade owns this mapping so consumers query by the model they see.
        if self.filter_value_maps or self.filter_op_maps:
            # first pass: which model key does each filter index target?
            key_by_index: dict[str, str] = {}
            for k, v in params:
                if k.startswith("filter[") and k.endswith("][key]"):
                    key_by_index[k[len("filter[") : k.index("]")]] = v
            translated = []
            for k, v in params:
                if k.startswith("filter[") and (k.endswith("][value]") or k.endswith("][op]")):
                    model_key = key_by_index.get(k[len("filter[") : k.index("]")])
                    maps = self.filter_value_maps if k.endswith("][value]") else self.filter_op_maps
                    vmap = maps.get(model_key or "")
                    if vmap:
                        v = vmap.get(v, v)
                translated.append((k, v))
            params = translated
        params = self._strip_reference_filter_prefixes(params)
        if self.query_aliases:
            translated = []
            for k, v in params:
                if k.endswith("[key]"):
                    v = self.query_aliases.get(v, v)
                elif k == "sort":
                    pre = "-" if v.startswith("-") else ""
                    f = v[1:] if pre else v
                    v = pre + self.query_aliases.get(f, f)
                translated.append((k, v))
            params = translated
        # Stable secondary sort on the flat (v3) path: append a deterministic
        # tiebreak so equal-primary-key rows keep a reproducible order across
        # calls and pages (F5). v1 strips sort entirely (below) and bf_sort has
        # its own shape (handled next); skip if the caller already sorts on it.
        if self.sort_tiebreak and not self.bf_sort and not self.v1_paging:
            out = []
            for k, v in params:
                if k == "sort" and self.sort_tiebreak not in v.split(","):
                    v = f"{v},{self.sort_tiebreak}"
                out.append((k, v))
            params = out
        if self.bf_sort:
            translated = []
            for k, v in params:
                if k == "sort":
                    pre = v.startswith("-")
                    translated.append(("sort[0][key]", v[1:] if pre else v))
                    translated.append(("sort[0][direction]", "desc" if pre else "asc"))
                else:
                    translated.append((k, v))
            params = translated
        if self.v1_paging and not upstream_handle:
            q = dict(params)
            number = q.get("page[number]") or "1"
            try:
                size = max(10, min(50, int(q.get("page[size]") or "25")))
            except ValueError:
                size = 25
            # v1 endpoints reject EVERY unexpected query key with a 400
            # ("Unexpected keys `sort`") — whitelist to paging + filters. The
            # generic table's sort/searchTerm are silently unsupported here (the
            # schema carries no sortable/searchable marks on v1 entities).
            params = [(k, v) for k, v in params if k.startswith("filter[")]
            params += [("page[number]", str(number)), ("page[size]", str(size))]
        if not any(k == "include" for k, _ in params) and self.include:
            params.append(("include", self.include))
        url = f"{base_url.rstrip('/')}{path}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.get(url, params=params, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

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
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        method = method.upper()
        # Gate every method against the entity's DECLARED operations, so an
        # undeclared capability can never reach the upstream (F10: DELETE was
        # dispatched on entities whose manifest lists only list/read/create/
        # update — silently allowing destructive deletes). The ops list is the
        # contract; a method outside it answers 405 instead of hitting Xentral.
        required_op = {
            "GET": "read" if handle else "list",
            "POST": "create",
            "PATCH": "update",
            "PUT": "update",
            "DELETE": "delete",
        }.get(method)
        if required_op and required_op not in self.manifest.operations:
            return self._json(
                405,
                {
                    "title": f"{self.manifest.key}: '{required_op}' is not supported",
                    "detail": (
                        f"This entity declares only {sorted(self.manifest.operations)}; "
                        f"'{required_op}' is not part of its contract."
                    ),
                },
            )
        if method == "DELETE":
            up = handle.split("_", 1)[1] if handle and "_" in handle else handle
            st, resp = await self._send(
                base_url, token, "DELETE", up, None, accept_language, client
            )
            return self._json(st, resp if isinstance(resp, dict) else {"data": {"id": handle}})
        if method in ("POST", "PATCH", "PUT"):
            return await self._write(
                method, handle, query, body, base_url, token, accept_language, client
            )
        # Consolidated `search` — the upstream has no cross-field search key
        # (it would 400 as "filter `search` not allowed"), so fan out over the
        # schema's `searchable` fields and merge. Only intercepts when the
        # entity declares fields; the metadata advertises `searchFields`, so
        # well-behaved consumers never send `search` to an entity without them.
        if handle is None:
            hit = extract_search(query)
            if hit is not None and self.search_fields():
                value, op = hit
                if value:
                    resp = await fan_out_search(
                        self,
                        query=query,
                        value=value,
                        op=op,
                        search_fields=self.search_fields(),
                        base_url=base_url,
                        token=token,
                        accept_language=accept_language,
                        client=client,
                    )
                    return self._searched_envelope(resp, query)
        status, payload = await self._get(
            base_url,
            token,
            handle=handle,
            query=query,
            accept_language=accept_language,
            client=client,
        )
        if status >= 400:
            return self._json(
                status, payload if isinstance(payload, dict) else {"title": "upstream error"}
            )
        if handle:
            rec = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rec, dict):
                return self._json(404, {"title": f"{self.manifest.key} {handle} not found"})
            return self._json(200, {"data": self.map_read(rec)})
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        mapped = [self.map_read(r) for r in rows if isinstance(r, dict)]
        return self._json(200, self._list_envelope(mapped, payload, query))

    def _searched_envelope(
        self, resp: AdapterResponse, query: list[tuple[str, str]]
    ) -> AdapterResponse:
        """Re-shape a fan-out search result into the shared list envelope.

        The rows are already model-mapped (the fan-out calls back into this
        adapter's ``request``); only the totals convention needs aligning."""
        if resp.status_code >= 400:
            return resp
        try:
            body = json.loads(resp.content or b"{}")
        except (ValueError, TypeError):
            return resp
        mapped = body.get("data") if isinstance(body, dict) else None
        return self._json(
            200,
            self._list_envelope(
                mapped if isinstance(mapped, list) else [],
                {"meta": body.get("meta") if isinstance(body, dict) else None},
                query,
            ),
        )

    @staticmethod
    def _query_paging(query: list[tuple[str, str]]) -> tuple[int, int]:
        q = dict(query)
        try:
            page = max(1, int(q.get("page[number]") or "1"))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = max(1, int(q.get("page[size]") or "25"))
        except (TypeError, ValueError):
            per_page = 25
        return page, per_page

    @staticmethod
    def _served_per_page(extra: dict[str, Any], up_meta: Any, requested: int) -> int:
        """The page size the upstream actually served, not the one we asked for.

        Asking is not getting: v1 caps ``perPage`` at 50 and quietly returns a
        short page. Echoing the *requested* size back makes ``lastPage`` too
        small by the same factor, and a consumer that pages to ``lastPage`` —
        the whole reason it is emitted — stops early with no error to notice.
        On the mvp tenant's 61,256 sales prices, ``perPage=100`` claimed 613
        pages instead of 1226, hiding half the data behind a number that looks
        authoritative.

        Upstream reports the size it used (v1 ``extra.page.size``, v3
        ``meta.perPage``), so take it from there and fall back to the request
        only when nothing is echoed.
        """
        echo = extra.get("page")
        echo = echo.get("size") if isinstance(echo, dict) else None
        if echo is None and isinstance(up_meta, dict):
            echo = up_meta.get("perPage")
        if isinstance(echo, int) and not isinstance(echo, bool) and echo > 0:
            return echo
        return requested

    def _list_envelope(
        self,
        mapped: list[dict[str, Any]],
        payload: Any,
        query: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Compose the list envelope with ONE total in both places.

        Consumers grew up on two conventions — ``extra.total`` (EntitySteckbrief,
        the Phoenix/Odoo cores) and ``meta.total`` (the v3 table envelope generic
        lists read for count + paging) — while the upstream reports the number in
        either place depending on the API generation (v3 → ``meta.total``, v1 →
        ``extra.totalCount``; some endpoints report none at all). Emitting both
        from the same source ends the overview-says-27-table-says-25 class of
        mismatches, and the ``page/perPage/lastPage`` block keeps "load more"
        paging working without consumers guessing from the row count.
        """
        extra = payload.get("extra") if isinstance(payload, dict) else None
        extra = dict(extra) if isinstance(extra, dict) else {}
        up_meta = payload.get("meta") if isinstance(payload, dict) else None
        total: int | None = None
        if isinstance(extra.get("total"), int):
            total = extra["total"]
        elif isinstance(up_meta, dict) and isinstance(up_meta.get("total"), int):
            total = up_meta["total"]
        elif isinstance(extra.get("totalCount"), int):
            total = extra["totalCount"]
        page, requested = self._query_paging(query)
        per_page = self._served_per_page(extra, up_meta, requested)
        meta: dict[str, Any] = {"page": page, "perPage": per_page}
        if total is not None:
            extra["total"] = total
            meta["total"] = total
            meta["lastPage"] = max(1, -(-total // per_page))
        return {"data": mapped, "meta": meta, "extra": extra}

    # ---- write orchestration --------------------------------------------
    def rejected_response(self, rejected: set[str]) -> AdapterResponse:
        """The 409 a write earns by naming fields the upstream cannot write today.
        A method rather than an inline body because adapters that compose a write
        outside the default path (salesOrder splits its line items off before
        delegating) must answer identically instead of dropping the rejection."""
        return self._json(
            409,
            {
                "title": f"{self.manifest.key}: fields not writable via the current Xentral API",
                "detail": (
                    "These fields are read-only upstream today (ADR-014: no overlay). "
                    "They are tracked as blue wishes in priorities.json."
                ),
                "fields": sorted(rejected),
            },
        )

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """Split a new-model write body into ``(v3_payload, rejected_paths)``.
        ``rejected_paths`` are fields the upstream cannot write today (ADR-014 →
        409, blue wish). Default rejects everything; adapters override with their
        real upstream coverage."""
        return {}, set(_flatten_paths(model))

    async def _send(
        self,
        base_url: str,
        token: str,
        method: str,
        up_handle: str | None,
        payload: dict[str, Any],
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> tuple[int, Any]:
        # Writes go to write_path when the adapter reads and writes different
        # upstream generations (Product); everyone else falls back to v3_path.
        path = (self.write_path or self.v3_path) + (f"/{up_handle}" if up_handle else "")
        url = f"{base_url.rstrip('/')}{path}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    async def _write(
        self,
        method: str,
        handle: str | None,
        query: list[tuple[str, str]],
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> AdapterResponse:
        try:
            model = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return self._json(400, {"title": "invalid JSON body"})
        if not isinstance(model, dict):
            return self._json(400, {"title": "body must be a JSON object"})
        v3_payload, rejected = self.map_write(model, creating=(method == "POST"))
        if rejected:
            return self.rejected_response(rejected)
        if any(k == "dryRun" and v in ("true", "1") for k, v in query):
            return self._json(200, {"data": {"dryRun": True, "wouldSend": v3_payload}})
        up_handle = handle.split("_", 1)[1] if handle and "_" in handle else handle
        st, resp = await self._send(
            base_url, token, method, up_handle, v3_payload, accept_language, client
        )
        if st >= 400:
            return self._json(
                st, resp if isinstance(resp, dict) else {"title": "upstream write error"}
            )
        new_id = up_handle or (
            (resp.get("data") or {}).get("id") if isinstance(resp, dict) else None
        )
        if new_id is not None:
            _, rpayload = await self._get(
                base_url,
                token,
                handle=str(new_id),
                query=[],
                accept_language=accept_language,
                client=client,
            )
            rec = rpayload.get("data") if isinstance(rpayload, dict) else None
            if isinstance(rec, dict):
                return self._json(201 if method == "POST" else 200, {"data": self.map_read(rec)})
        return self._json(201 if method == "POST" else 200, resp if isinstance(resp, dict) else {})

    async def _tag_action(
        self,
        action_key: str,
        ids: list[Any],
        command: dict[str, Any],
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> AdapterResponse:
        """Generic addTag/removeTag: a v3 update REPLACES the whole tag list, so
        this is a read-modify-write through the adapter's own read/write mapping
        (net effect: exactly one tag added or removed).

        Effect-checked: current Xentral builds auto-create an unknown title on
        the v3 write (verified live on mvp for salesOrders/customers/suppliers/
        deliveryNotes), but older builds answer 200 and silently DROP titles
        missing from the tag catalogue. The action promises "created
        automatically if new", so after the PATCH we verify the read-back; if
        the tag did not stick we create it in the BF catalogue
        (``POST /api/entity/tag``) and retry once — and answer an honest error
        instead of a false success when it still does not persist."""
        title = str(command.get("title") or "").strip()
        if not title:
            return self._json(422, {"title": f"{action_key} requires a non-empty 'title'."})
        if not ids:
            return self._json(422, {"title": f"{action_key} needs a target id (ids[])"})
        handle = str(ids[0])
        current = await self.request(
            method="GET",
            handle=handle,
            query=[],
            body=None,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if current.status_code >= 400:
            return current
        try:
            record = json.loads(current.content or b"{}").get("data") or {}
        except ValueError:
            record = {}
        titles = [t for t in (record.get("tags") or []) if isinstance(t, str)]
        if action_key == "addTag":
            if title not in titles:
                titles.append(title)
        else:
            titles = [t for t in titles if t != title]

        async def _patch() -> AdapterResponse:
            return await self.request(
                method="PATCH",
                handle=handle,
                query=[],
                body=json.dumps({"tags": titles}).encode(),
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )

        patched = await _patch()
        if patched.status_code >= 400 or self._tag_effect_ok(patched, action_key, title):
            return patched
        create_error: str | None = None
        if action_key == "addTag":
            create_error = await self._create_catalogue_tag(
                title, base_url, token, accept_language, client
            )
            if create_error is None:
                retried = await _patch()
                if retried.status_code >= 400 or self._tag_effect_ok(retried, action_key, title):
                    return retried
        verb = "attach" if action_key == "addTag" else "remove"
        return self._json(
            502,
            {
                "title": (
                    f"{action_key}: the upstream accepted the write but did not "
                    f"{verb} tag '{title}'."
                ),
                "detail": (
                    "The Xentral v3 API answered 200 without persisting the tag "
                    "change (older builds silently drop titles missing from the "
                    "tag catalogue)."
                    + (
                        f" Creating the catalogue tag also failed — {create_error}"
                        if create_error
                        else ""
                    )
                ),
            },
        )

    @staticmethod
    def _tag_effect_ok(response: AdapterResponse, action_key: str, title: str) -> bool:
        """Did the tag change actually stick? Only a POSITIVE sighting of a tags
        list in the wrong state counts as failure — an unparsable/tag-less
        response is inconclusive and passes through unchanged."""
        try:
            data = json.loads(response.content or b"{}").get("data") or {}
        except (ValueError, AttributeError):
            return True
        tags = data.get("tags") if isinstance(data, dict) else None
        if not isinstance(tags, list):
            return True
        present = title in [t for t in tags if isinstance(t, str)]
        return present if action_key == "addTag" else not present

    async def _create_catalogue_tag(
        self,
        title: str,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> str | None:
        """Create ``title`` in the BF tag catalogue so a v3 tags write can match
        it. ``POST /api/entity/tag`` requires label + slug (verified live: 201
        with uuid; missing slug → 400 "slug is required"). Returns None on
        success, an error detail string on failure."""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or title.lower()
        url = f"{base_url.rstrip('/')}/api/entity/tag"
        headers = self._headers(token, accept_language)
        payload = {"label": title, "slug": slug}

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.post(url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        if resp.status_code < 400:
            return None
        return f"POST /api/entity/tag → HTTP {resp.status_code}: {resp.text[:300]}"

    # {step/action key: upstream route}. Two forms:
    #   tuple ("PATCH", "cancel")   → {v3_path}/{id}/actions/cancel (v3 record action)
    #   dict  {"method": "POST", "path": "/api/v3/invoices/actions/createFromSalesOrder",
    #          "body": {"salesOrder": {"id": "{id}"}}}
    #         → explicit path (may contain {id}); the body template has "{id}"
    #           placeholders filled with the upstream id, then the caller's
    #           command is shallow-merged over it.
    action_map: dict[str, Any] = {}

    def _wish_reason(self, action_key: str) -> str | None:
        """The wish reason of a declared-but-unimplemented step/action (ADR-014:
        the model promises it, no upstream exists yet → 409 + blue wish)."""
        for a in self.actions():
            if a.get("key") == action_key and a.get("wish"):
                return str(a["wish"])
        for group in self.steps():
            for cmd in group.get("commands") or []:
                if cmd.get("key") == action_key and cmd.get("wish"):
                    return str(cmd["wish"])
        return None

    @staticmethod
    def _fill_body(template: Any, up_id: str) -> Any:
        if isinstance(template, dict):
            return {k: FacadeAdapterBase._fill_body(v, up_id) for k, v in template.items()}
        if isinstance(template, list):
            return [FacadeAdapterBase._fill_body(v, up_id) for v in template]
        if template == "{id}":
            return up_id
        return template

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        # The gateway passes the target id(s) in the {ids, command} envelope.
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if action_key in ("addTag", "removeTag"):
            return await self._tag_action(
                action_key,
                ids,
                envelope.get("command") or {},
                base_url,
                token,
                accept_language,
                client,
            )
        route = self.action_map.get(action_key)
        if route is None:
            wish = self._wish_reason(action_key)
            if wish:
                return self._json(
                    409,
                    {
                        "title": f"{self.manifest.key}: '{action_key}' is not available upstream yet",
                        "detail": wish,
                        "wish": True,
                    },
                )
            return self._json(
                501,
                {
                    "title": f"{self.manifest.key}: '{action_key}' not wired to an upstream action yet",
                    "detail": "No upstream v3 action maps to this step/action (see docs/03-mapping-layer.md).",
                },
            )
        if not ids:
            return self._json(422, {"title": f"{action_key} needs a target id (ids[])"})
        up_id = str(ids[0]).split("_", 1)[1] if "_" in str(ids[0]) else str(ids[0])
        command = envelope.get("command") or {}
        if isinstance(route, dict):
            method = route.get("method", "POST")
            url = f"{base_url.rstrip('/')}{route['path'].replace('{id}', up_id)}"
            payload = self._fill_body(route.get("body") or {}, up_id)
            payload.update(command)
            json_body: Any = payload or None
        else:
            method, v3_key = route
            url = f"{base_url.rstrip('/')}{self.v3_path}/{up_id}/actions/{v3_key}"
            json_body = command or None
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.request(method, url, json=json_body, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        if resp.status_code >= 400:
            return AdapterResponse(
                resp.status_code, resp.content, {"content-type": "application/json"}
            )
        try:
            result = resp.json()
        except ValueError:
            result = None
        _, rpayload = await self._get(
            base_url, token, handle=up_id, query=[], accept_language=accept_language, client=client
        )
        rec = rpayload.get("data") if isinstance(rpayload, dict) else None
        out: dict[str, Any] = {
            "data": self.map_read(rec) if isinstance(rec, dict) else {"id": ids[0]}
        }
        # Cross-entity creates (invoice from sales order, …) return the created
        # record — surface it so the caller can follow up on it.
        if isinstance(result, dict) and result.get("data"):
            out["result"] = result["data"]
        return self._json(200, out)


def _flatten_paths(obj: Any, prefix: str = "") -> list[str]:
    """Dotted leaf paths of a nested dict (used by the default map_write reject)."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out += _flatten_paths(v, p)
            else:
                out.append(p)
    return out


# Shared status-mapper helper: {upstream: new} with a default passthrough.
def status_map(mapping: dict[str, str], value: Any, default: str | None = None) -> str | None:
    if value in (None, ""):
        return default
    return mapping.get(str(value), default if default is not None else str(value))


# A read-only computed marker used a lot in the model (totals, holds, documents…).
RO: dict[str, Any] = {"access": "readOnly"}

# Callable type alias for adapters that build sub-trees.
FieldBuilder = Callable[[], dict[str, dict[str, Any]]]
