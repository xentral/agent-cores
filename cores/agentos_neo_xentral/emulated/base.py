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
import base64
import json
import os
import re

import yaml

from collections.abc import Callable, Iterator
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

# Shared consolidated-`search` machinery (OR fan-out over per-field contains
# filters, verified live against the same v3 endpoints) — the xentral_api core
# owns the implementation; reusing it keeps ONE search behavior across cores
# (weclapp_core → agentos_neo_weclapp precedent for cross-core imports).
from xentral_entity_cores.xentral_api.emulated._search import (
    extract_search,
    fan_out_search,
    strip_search,
)

from ..verdicts import is_proven

_TIMEOUT = 60.0
_UA = "xentral-ai-agent"
# A speaking id — ``<prefix>_<numeric>`` (eid(); e.g. ``cus_20423``, ``prd_61617``).
# Reference filter values arrive in this shape but upstream filters on the bare
# numeric id, so the prefix is stripped for reference-typed filter keys.
_SPEAKING_ID = re.compile(r"^[a-z]+_[0-9]+$")


@functools.lru_cache(maxsize=1)
def _field_gaps() -> dict[str, Any]:
    """Per-entity field gaps from the core's field-gaps.yaml — the field axis of the
    specification (docs/03-mapping-layer.md §5). Missing file → empty.

    YAML rather than JSON because the payload is prose: each entry carries a business
    reason that a reviewer writes and edits, and JSON can neither comment it nor wrap
    it. Parsed once per process (lru_cache), so the slower parser costs nothing.
    """
    path = os.path.join(os.path.dirname(__file__), "..", "field-gaps.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (FileNotFoundError, ValueError, yaml.YAMLError):
        return {}


@functools.lru_cache(maxsize=1)
def _wish_reasons() -> dict[str, Any]:
    """Why each declared-but-not-executable capability cannot run, from the core's
    capabilities.spec.yaml (``wishes`` → ``<Entity>`` → ``<key>`` → reason).

    The adapters say WHICH capabilities are gaps; this file says WHY. Keeping the two
    apart is what lets the playbook test compare them: a spec that also decided the
    classification would be checking itself. The text lives here because it is a
    business statement the specification's owner must be able to edit without touching
    Python. Missing file → empty, and `_wish_reason` then names the omission.
    """
    path = os.path.join(os.path.dirname(__file__), "..", "capabilities.spec.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("wishes") or {}
    except (FileNotFoundError, ValueError, yaml.YAMLError):
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


def id_from_location(location: str | None) -> str | None:
    """The new record's id out of a ``Location`` header, in the two shapes upstream
    actually uses.

    v2 products answer ``…/api/v2/products/61997`` — the id is the last segment.
    v1 warehouses answer ``…/api/warehouses?filter[0][key]=id&filter[0][op]=equals
    &filter[0][value]=43`` — a QUERY that finds the record rather than a link to it.
    Both are a 201 with an empty body, so without this the create flow has nothing
    to read back and the caller gets a success with no id.
    """
    if not location:
        return None
    head, _, query = location.partition("?")
    if query:
        for part in query.split("&"):
            key, _, value = part.partition("=")
            # The key itself may be percent-encoded — warehouses answer
            # `filter[0][value]=43`, their storage locations
            # `filter%5B0%5D%5Bvalue%5D=258`. Comparing the raw key finds the first
            # and misses the second, which is how the id came back as the whole
            # query string.
            if unquote(key).endswith("[value]") and value:
                return unquote(value)
    tail = head.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


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
    None when there is no amount to send.

    The currency is ALWAYS the document's — a position cannot carry one of its own.
    Line money is still emitted as ``{amount, currency}`` on read, because every
    money value in the model has that shape (ADR-006), but the header decides. So a
    caller who sends a deviating currency is not forwarded into an upstream 400 for
    something the schema never marked writable in the first place."""
    up = item.get("unitPrice")
    amount = up.get("amount") if isinstance(up, dict) else up
    if amount is None:
        return None
    return {"net": {"amount": str(amount), "currency": currency}}


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


def item_totals_prop() -> dict[str, Any]:
    """A line's totals for the whole quantity — see ``map_item_totals``. No ``tax``
    leaf: v3 states no per-line tax amount and the core does not invent one."""
    return prop(
        "embedded",
        "Item totals",
        access="readOnly",
        properties={
            "net": prop("string", "Net", access="readOnly"),
            "gross": prop("string", "Gross", access="readOnly"),
        },
    )


def contribution_margin_prop() -> dict[str, Any]:
    """A line's contribution margin (Deckungsbeitrag). Upstream reports a PERCENT,
    not an amount — verified on mvp: net 200 with an EK of 50 reports 75, and net 100
    with an EK of 40 reports 60, i.e. (net - EK) / net × 100. Computed upstream from
    the line's purchasePrice, so it moves when the EK is written."""
    return prop("decimal", "Contribution margin %", access="readOnly")


def line_purchase_price_net(item: dict[str, Any], currency: str = "EUR") -> dict[str, Any] | None:
    """v3 ``purchasePrice`` payload from a line item's ``purchasePrice``, tolerating
    both the canonical ``{"amount": …, "currency": …}`` object AND a bare scalar
    amount. None when there is no amount to send — upstream rejects an explicit null
    (its EK columns are NOT NULL), so a cleared EK is never emitted.

    The currency is ALWAYS the document's, as for the sale price: upstream refuses an
    EK whose currency differs from the document's, and the position has no currency
    of its own to differ with."""
    pp = item.get("purchasePrice")
    amount = pp.get("amount") if isinstance(pp, dict) else pp
    if amount is None:
        return None
    return {"net": {"amount": str(amount), "currency": currency}}


def map_purchase_price(li: dict[str, Any], currency: str = "EUR") -> dict[str, Any] | None:
    """Read a v3 line item's ``purchasePrice.net`` back into the model's flat money
    shape. None when upstream reports no EK."""
    net = (li.get("purchasePrice") or {}).get("net") or {}
    return money(net.get("amount"), net.get("currency") or currency)


def map_item_totals(li: dict[str, Any], currency: str = "EUR") -> dict[str, Any] | None:
    """A line's totals for the whole quantity, passed straight through from upstream's
    ``lineItemRevenue``.

    Upstream carries BOTH a per-unit and a quantity-total revenue, and the published
    OpenAPI descriptions have them the wrong way round (corrected in the monorepo,
    not yet in the spec repo). Verified on mvp with quantity 3 × net 100:
    ``itemRevenue`` reported 100 and ``lineItemRevenue`` 300 — so the quantity total,
    which is what a line's ``totals`` means, is ``lineItemRevenue``.

    There is deliberately NO ``tax`` here. v3 exposes no per-line tax amount (only
    ``taxRate`` and ``effectiveTaxRate``), and deriving it as gross - net would be the
    core computing a money figure Xentral never stated — against ADR-014's 1:1
    pass-through, and squarely on the unresolved legacy rounding-parity question
    (docs/03-mapping-layer.md Kategorie 3 §1, ``projekt.preisberechnung``). A derived
    number would be indistinguishable from an upstream one while being free to
    disagree with the printed document. The gap is a blue wish in field-gaps.yaml;
    per-rate tax is available on the document's ``totals``."""
    rev = li.get("lineItemRevenue") or {}
    net = ((rev.get("net") or {}).get("amount"), (rev.get("net") or {}).get("currency"))
    gross = ((rev.get("gross") or {}).get("amount"), (rev.get("gross") or {}).get("currency"))
    if net[0] is None and gross[0] is None:
        return None
    cur = net[1] or gross[1] or currency
    return {
        "net": (money(net[0], cur) or {}).get("amount"),
        "gross": (money(gross[0], cur) or {}).get("amount"),
    }


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


def tags_prop(*, writable: bool = False, filterable: bool = True) -> dict[str, Any]:
    """The model's ``tags`` field: a plain string array of tag titles
    (docs/01-model.md §6.1 ``"tags": ["b2b", "vip"]``). Declared as the FE's
    ``tag`` type — that is the contract for the pill renderer AND for the table
    column picker (``collection`` is excluded from columns). v3 accepts titles on
    write, so the string form round-trips; color/group live on the Tag entity."""
    # Not every upstream list accepts a tag filter — v3 products rejects it
    # outright, so the entity that cannot filter says so instead of promising it.
    # The documents DO filter by tag (measured on mvp: one tagged sales order in,
    # one row out). An earlier probe read as "declared but broken" only because the
    # tagged order was a DRAFT, and the v3 list endpoints exclude drafts unless the
    # status filter is set explicitly — see the default-status trap in the playbook.
    flags: dict[str, Any] = {"filterable": True} if filterable else {}
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


# Facets whose verdict describes THIS facade's model rather than the upstream, so a
# proof there cannot answer a wish about what the upstream can do. `read` is the
# whole set: the mapping layer synthesises, derives and composes values, so seeing
# one says nothing about where it came from. See `_proven`.
_MODEL_ONLY_FACETS = frozenset({"read"})


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
    # ``_send`` (POST/PATCH/PUT) targets ``write_path or v3_path``; reads (_get)
    # always use v3_path.
    write_path: str = ""
    # …and a THIRD generation can own the delete. Product reads /api/v3/products,
    # writes /api/v2/products and deletes /api/products/{id} — measured: a DELETE on
    # the v2 write path answers 404 "Route not found", on v1 it answers 204 and the
    # record is really gone. Empty falls back to the write path, which is right for
    # every entity whose upstream keeps writes and deletes together.
    delete_path: str = ""
    include: str = ""
    sections: dict[str, dict[str, str]] = {}
    preview_template: str = "{{number}}"
    # v1 list endpoints (salesPrices, shipments, storageLocations, inventoryRuns,
    # pickLists…) REQUIRE page[number] AND page[size] with size 10..50; setting
    # this flag makes ``_get`` guarantee both (clamping the requested size).
    v1_paging: bool = False
    # The document PATCH cannot carry line items on any v3 business document, but
    # ``{v3_path}/{id}/lineItems`` is a full sub-resource (POST/PATCH/DELETE). Set
    # this and ``update`` with ``items`` reconciles there as a collection replace —
    # see ``_reconcile_line_items``. Requires the adapter to implement
    # ``_item_to_v3`` and to declare ``items`` in ``fields()``.
    reconciles_line_items: bool = False
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
        wish: bool = False,
        command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One entry of the model's action catalogue (docs/01-model.md §8).

        ``wish=True`` marks a planned-but-unimplemented action: it is DECLARED (the
        catalogue is the contract) but executing it returns 409 with the reason —
        the action-side twin of the blue field gaps (ADR-014).

        The adapter states only THAT a capability is a gap; the reason text lives in
        ``capabilities.spec.yaml``, where the reviewer who owns the requirement can
        edit it. Those texts are business statements ("the transition happens only in
        the UI"), and they used to sit in this Python file where that reviewer could
        not reach them.

        The classification deliberately stays here. If the spec decided *which*
        capabilities are gaps, the two rules that carry the most weight in
        ``test_core_playbooks`` — a required capability must not be a wish, a
        recorded gap must still be one — would be comparing the spec against itself.
        Two independent statements are what makes the comparison worth running.
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
            d["wish"] = self._spec_wish_reason(key)
        return d

    def step_cmd(self, key: str, label: str, *, wish: bool = False) -> dict[str, Any]:
        """One command of a process step. ``wish=True`` as in ``action_def``.

        No longer a ``@staticmethod``: resolving the reason needs the entity key, and
        every call site already goes through ``self``.
        """
        cmd: dict[str, Any] = {"key": key, "label": label}
        if wish:
            cmd["wish"] = self._spec_wish_reason(key)
        return cmd

    def _spec_wish_reason(self, key: str) -> str:
        """The recorded reason a capability cannot be executed, from the spec.

        A gap with no reason is not a gap, it is an omission — nobody can tell whether
        the upstream cannot do it or nobody looked. So a missing entry says exactly
        that rather than rendering an empty string; ``test_every_wish_has_a_reason``
        fails the build on it.
        """
        # Defensive on shape, not just on absence. This file is now on the runtime
        # path, so a malformed section must degrade to the placeholder rather than
        # raise: a spec typo should cost a reason string, never a 500 from `describe`.
        # `test_every_wish_carries_a_reason` is what turns it back into a build error.
        entity = _wish_reasons().get(self.manifest.key)
        reason = entity.get(key) if isinstance(entity, dict) else None
        return (
            reason
            if isinstance(reason, str) and reason.strip()
            else (
                f"Declared as not executable, but capabilities.spec.yaml records no reason "
                f"for {self.manifest.key}.{key}."
            )
        )

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

    def _verified_fields(self) -> dict[str, Any]:
        """This entity's recorded field verdicts. The seam a test overrides to inject
        data — so the RULE in ``_proven`` is only ever written once and a stub cannot
        drift away from it."""
        return (_verified().get(self.manifest.key) or {}).get("fields") or {}

    def _proven(self, field: str, op: str) -> bool:
        """Whether a LIVE probe has shown this op working on this field.

        Declaration is the wrong yardstick here. `Product.suppliers` declares a
        writable child (the default supplier maps to v2 standardSupplier) while
        multi-supplier sourcing — what the wish is actually about — has no write
        path at all; dropping that wish because a flag exists would erase a real
        gap. A recorded proof cannot be argued with: something wrote the value and
        read it back.

        A container counts as proven when any of its leaves is: `items` is editable
        exactly when a line field has been shown to update.

        **`read` cannot retire a wish, and the reason is not the one it used to be.**
        The probe once stamped `read: pass` on every declared path before looking at
        a payload, and taking that as proof retired 50 legitimate wishes in one run.
        That is fixed — a read verdict now means a real record carried a value. But
        it carried it *in THIS facade's model*, and the model invents values:
        `Customer.addresses.isDefault` is a hard-coded `True` on the main address,
        `addresses.label` a literal `"Hauptadresse"`, `Channel.platform` a string
        match against `moduleName`. A read wish disputes what the UPSTREAM supplies,
        which observing our own output cannot answer. Measured: the first live run
        under the new vocabulary reported 14 read wishes as obsolete, and every one
        of them was still true.

        The write and query facets are different in kind and do count: they reach
        the upstream and come back — a filter it does not support cannot return the
        record the value came from, and a value we wrote and read back was stored
        somewhere real."""
        if op in _MODEL_ONLY_FACETS:
            return False
        fields = self._verified_fields()
        if is_proven((fields.get(field) or {}).get(op)):
            return True
        prefix = f"{field}."
        return any(
            path.startswith(prefix) and is_proven((facets or {}).get(op))
            for path, facets in fields.items()
        )

    def _apply_field_gaps(self, properties: dict[str, Any]) -> None:
        """Stamp the hand-curated blue wishes (field-gaps.yaml) onto the schema —
        the living backlog (docs/03-mapping-layer.md §5). Renders blue only where
        the op is actually unavailable.

        A wish outranks every other verdict where it is shown, including a `pass` —
        right for a real gap, dangerous for a stale one: the entry goes on claiming
        "not possible" over a capability that has since been built and live-proven,
        and the proof is what gets hidden. An op a live probe has PROVEN is
        therefore not stamped, and the entry is surfaced through obsolete_wishes()
        so it gets deleted rather than quietly ignored."""
        for field, ops in self._wishes_by_field().items():
            spec = self._resolve_path(properties, field) if "." in field else properties.get(field)
            if not isinstance(spec, dict):
                continue
            live = {op: reason for op, reason in ops.items() if not self._proven(field, op)}
            if live:
                spec["priority"] = live

    def _wishes_by_field(self) -> dict[str, dict[str, str]]:
        by_field: dict[str, dict[str, str]] = {}
        for entry in _field_gaps().get(self.manifest.key) or ():
            field = entry.get("field")
            reason = entry.get("reason") or ""
            for op in entry.get("ops") or ():
                by_field.setdefault(field, {})[op] = reason
        return by_field

    def obsolete_wishes(self) -> list[dict[str, Any]]:
        """Wishes whose op the schema now declares — the backlog contradicting the
        core. Reported by validate_cores.py so the entry is deleted, not carried."""
        properties = self.fields()
        out: list[dict[str, Any]] = []
        for field, ops in self._wishes_by_field().items():
            spec = self._resolve_path(properties, field) if "." in field else properties.get(field)
            if not isinstance(spec, dict):
                continue
            done = sorted(op for op in ops if self._proven(field, op))
            if done:
                out.append({"field": field, "ops": done})
        return out

    def missing_field_wishes(self) -> list[dict[str, Any]]:
        """Wishes for a field the schema does not have at all.

        These are the widest gaps in the backlog — `items.totals.tax` (no per-line
        tax amount exists upstream), `contacts.address` — and until now they
        rendered nowhere: there is no row to colour blue, so the stamp silently
        found no spec and the wish vanished from every capability view."""
        properties = self.fields()
        out: list[dict[str, Any]] = []
        for field, ops in sorted(self._wishes_by_field().items()):
            spec = self._resolve_path(properties, field) if "." in field else properties.get(field)
            if not isinstance(spec, dict):
                out.append({"field": field, "ops": sorted(ops), "reason": next(iter(ops.values()))})
        return out

    def _apply_verified(self, properties: dict[str, Any]) -> None:
        """Stamp live-test results (verified.json) onto the schema — the green/red
        side of the grid. ``fields.<path>`` → per-facet pass/fail (+ <op>Note)."""
        fields = (_verified().get(self.manifest.key) or {}).get("fields") or {}
        for path, facets in fields.items():
            spec = self._resolve_path(properties, path) if "." in path else properties.get(path)
            if isinstance(spec, dict) and isinstance(facets, dict):
                spec["verified"] = facets

    def _apply_verified_capabilities(
        self, entries: list[dict[str, Any]], results_key: str, notes_key: str
    ) -> None:
        """Stamp live-test results onto ACTIONS / process-step commands.

        Only fields were ever stamped, so every action in every capability view read
        "declared, untested" — including the ones a live run had proven. SalesOrder
        carried `createSalesInvoice: pass` in verified.json while its sheet said
        `offen` for all thirteen."""
        entity = _verified().get(self.manifest.key) or {}
        results = entity.get(results_key) or {}
        notes = entity.get(notes_key) or {}
        for entry in entries:
            key = entry.get("key")
            if key in results:
                entry["verified"] = {"status": results[key]}
                if notes.get(key):
                    entry["verified"]["note"] = notes[key]

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

    def _apply_detail_only(self, properties: dict[str, Any]) -> None:
        """Stamp ``detailOnly`` onto the sections a list does not load.

        The response already says it after the fact (``extra.unavailableSections``),
        which is too late to plan with: a caller looking for "products under 10 €"
        lists, sees ``prices.sale: null`` on every row and draws the wrong
        conclusion. In the schema the same fact is available BEFORE the call — read
        this record singly, or do not ask for this field in a list.

        Derived from ``detail_only_sections`` so the declaration stays single;
        stamped on the section and everything below it, because a consumer may look
        only at the leaf it wants.
        """

        def stamp(spec: Any) -> None:
            if not isinstance(spec, dict):
                return
            spec["detailOnly"] = True
            sub = spec.get("properties")
            if not isinstance(sub, dict):
                sub = (spec.get("node") or {}).get("properties")
            for child in (sub or {}).values():
                stamp(child)

        for path in self.detail_only_sections:
            stamp(self._resolve_path(properties, path) if "." in path else properties.get(path))

    def _filterable_keys(self) -> set[str]:
        """Every MODEL path the schema marks ``filterable``, cached per class."""
        cached = type(self).__dict__.get("_filterable_keys_cache")
        if cached is not None:
            return cached

        def walk(props: dict[str, Any], prefix: str = "") -> Iterator[str]:
            for name, spec in (props or {}).items():
                if not isinstance(spec, dict):
                    continue
                path = f"{prefix}.{name}" if prefix else name
                if spec.get("filterable"):
                    yield path
                sub = spec.get("properties")
                if not isinstance(sub, dict):
                    sub = (spec.get("node") or {}).get("properties")
                if isinstance(sub, dict):
                    yield from walk(sub, path)

        try:
            keys = set(walk(self.fields()))
        except Exception:  # noqa: BLE001 - a broken fields() must not break list
            keys = set()
        type(self)._filterable_keys_cache = keys
        return keys

    def _undeclared_filter_keys(self, query: list[tuple[str, str]]) -> list[str]:
        """Filter keys the caller sent that this entity does not declare.

        Passing an unknown filter on to the upstream is not harmless: several v3
        list endpoints (merchandiseGroups, productCategories, webhooks, …) IGNORE
        it and answer 200 with the whole collection. The caller then reads an
        unfiltered list as a filtered one — verified on mvp, where filtering
        merchandise groups by a nonexistent name returned all 15 rows. A loud
        422 here is strictly better than a plausible wrong answer there.

        ``searchable`` keys are allowed through: the consolidated search fans out
        over exactly those, and it calls back into this method.

        The ``search`` key itself is exempt only where that fan-out EXISTS. An
        entity with no searchable field never intercepts the key (``request``
        checks ``search_fields()`` before fanning out), so an exemption would hand
        ``search`` straight to the upstream as an undeclared filter — the trap
        above, reached through the one key that was allowed to skip the guard.
        Most of this core is in that position: 31 of 47 adapters declare no
        searchable field.
        """
        allowed = self._filterable_keys() | set(self.search_fields())
        exempt = {"search"} if self.search_fields() else set()
        sent = {
            v
            for k, v in query
            if k.startswith("filter[") and k.endswith("][key]") and v not in exempt
        }
        return sorted(sent - allowed)

    def refuse_undeclared_filters(self, query: list[tuple[str, str]]) -> AdapterResponse | None:
        """The 422 for an undeclared filter key, or ``None`` when the query is clean.

        Public because an adapter that overrides ``request`` for its list path
        builds its own upstream call and would otherwise skip the guard entirely —
        which is how ``TaxRate`` and ``TextTemplate`` kept forwarding (and silently
        DROPPING) unknown keys. Call this before building that call.
        """
        refused = self._undeclared_filter_keys(query)
        if not refused:
            return None
        detail = (
            "This entity does not filter on "
            f"{', '.join(refused)}. Some upstream list endpoints "
            "IGNORE an unknown filter and answer 200 with the "
            "unfiltered collection, which reads as a filtered "
            "result — so an undeclared key is refused here instead "
            "of being passed on."
        )
        if "search" in refused:
            detail += (
                " `search` is refused because this entity declares no searchable "
                "field: there is nothing to fan out over, so the key would reach "
                "the upstream as an undeclared filter. See `searchFields` in the "
                "entity metadata for the entities that do support it."
            )
        return self._json(
            422,
            {
                "title": f"{self.manifest.key}: filter(s) not supported",
                "detail": detail,
                "filterable": sorted(self._filterable_keys()),
                "searchable": sorted(self.search_fields()),
            },
        )

    def search_fields(self) -> tuple[str, ...]:
        """MODEL paths flagged ``searchable`` in the entity's schema — the
        consolidated ``search`` filter fans out over exactly these, so the
        search contract lives next to the field declarations.

        Nested paths count. This walked only the top level until now, so twelve
        leaves the schema advertises as searchable were never reached by a search:
        `Customer`/`Supplier` `addresses.{street,zip,city,state,country}`,
        `Product.identifiers.ean`, `PurchaseInvoice.references.supplierInvoiceNumber`
        — the address and barcode fields a merchant actually searches by. On
        `PurchaseInvoice` the fan-out was empty altogether, so it had no search at
        all while its schema said otherwise.

        Measured on mvp before switching this on: every one of the twelve answers a
        `contains` filter with 200, returns the record the value came from, and
        narrows the set (customers 20128 → 8 for a city). The fan-out builds exactly
        that query per field, so a declared path now behaves the way it reads.
        """
        cached = type(self).__dict__.get("_search_fields_cache")
        if cached is not None:
            return cached

        def walk(props: dict[str, Any], prefix: str = ""):
            for name, spec in (props or {}).items():
                if not isinstance(spec, dict):
                    continue
                path = f"{prefix}{name}"
                if spec.get("searchable"):
                    yield path
                sub = spec.get("properties")
                if not isinstance(sub, dict):
                    node = spec.get("node")
                    sub = node.get("properties") if isinstance(node, dict) else None
                if isinstance(sub, dict):
                    yield from walk(sub, f"{path}.")

        fields = tuple(walk(self.fields()))
        type(self)._search_fields_cache = fields
        return fields

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        properties = self.fields()
        self._apply_field_gaps(properties)
        self._apply_verified(properties)
        self._apply_descriptions(properties)
        self._apply_detail_only(properties)
        meta: dict[str, Any] = {
            "key": self.manifest.key,
            "label": self.manifest.label(accept_language),
            # Which group the entity is filed under (documents / masterdata /
            # crm / settings). It rides along here, not only in the catalogue,
            # because a consumer that already knows the key goes straight to
            # this schema — and "settings" is what tells it this is a
            # configuration catalogue to read valid values from rather than a
            # business record to work on.
            "category": self.manifest.category,
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
        # Gaps with no field to colour blue: the schema has no such path, so the
        # wish would otherwise be invisible in every capability view. Named here so
        # "this entity cannot express X at all" is readable, not just "X is absent".
        missing = self.missing_field_wishes()
        if missing:
            meta["missingFieldWishes"] = missing
        # Advertise what a search actually MATCHES — consumers (record pickers, list
        # search) key their server-search affordance off this list. Where the
        # upstream searches natively that is ITS field set, not the schema flags the
        # fan-out would have used: our document adapters flag `number` alone, while
        # the native search also reaches the document address and the customer's
        # order number, so the flags would understate it by five fields.
        searchable = list(self.native_search_fields or self.search_fields())
        if searchable:
            meta["searchFields"] = searchable
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
        # Declaring the `writeProtection` field opts an entity into the two v3
        # actions that flip it — the same "the field is the switch" convention the
        # tag actions use above. v3 ships them on every business document type
        # (setWriteProtection / removeWriteProtection); the routes live in each
        # adapter's action_map, the catalogue text stays here so the wording cannot
        # drift across seven documents.
        if isinstance(properties.get("writeProtection"), dict):
            wp_path = f"/api/entity/{self.manifest.key}/actions"
            actions += [
                {
                    "key": "setWriteProtection",
                    "label": "Set write protection",
                    "bulk": False,
                    "method": "PATCH",
                    "path": f"{wp_path}/setWriteProtection",
                    "destructive": False,
                    "description": (
                        "Protect this document against changes: an update then answers "
                        "409 write-protected. TWO fields still go through — the internal "
                        "note and the status (upstream's writeProtectionBypassFields), so "
                        "a successful note write is NOT evidence the document is "
                        "unprotected. Read the `writeProtection` field for that."
                    ),
                },
                {
                    "key": "removeWriteProtection",
                    "label": "Remove write protection",
                    "bulk": False,
                    "method": "PATCH",
                    "path": f"{wp_path}/removeWriteProtection",
                    "destructive": False,
                    "description": "Lift the write protection so the document can be edited again.",
                },
            ]
        if actions:
            self._apply_verified_capabilities(actions, "actions", "actionsNotes")
            meta["actions"] = actions
        steps = self.steps()
        if steps:
            # Commands live one level down, inside their group.
            for group in steps:
                self._apply_verified_capabilities(
                    group.get("commands") or [], "processSteps", "processStepsNotes"
                )
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

    @classmethod
    def _refuse(cls, status: int, title: str, **extra: Any) -> AdapterResponse:
        """A refusal the CORE decided, before the upstream was ever called.

        Marked `source: "core"` so a caller can tell it from an upstream rejection.
        They look identical otherwise, and the difference is load-bearing: the
        capability probe grades a 4xx as "the route exists and validated my request",
        which is a claim about the UPSTREAM. Every 422 in the committed manifest
        turned out to come from here instead, so nine action verdicts rested on our
        own input validation having run.
        """
        return cls._json(status, {"title": title, "source": "core", **extra})

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

    # Some upstream collections parse a `datetime` filter as a DATE and reject
    # anything carrying a time — including the very timestamp they return on read:
    #
    #   /api/v3/customers  filter[createdAt]=2024-11-21T04:16:22+01:00 -> 400 "not a valid date"
    #                      filter[createdAt]=2024-11-21                -> 200
    #   /api/v3/salesOrders            the exact opposite (400 "not a valid datetime")
    #
    # Both families READ back a full ISO timestamp, so on the partner endpoints a
    # caller cannot filter on the value they were just handed. That is an upstream
    # inconsistency and is reported as such; until it is fixed the facade absorbs
    # it, because the model promises ONE `createdAt: datetime, filterable` across
    # every entity and a consumer must not have to know which parser sits behind
    # which key. This trims the caller's value to its date part — a format
    # adaptation at the boundary, not invented data (ADR-014 forbids the latter).
    # Remove the flag once the upstream filter accepts what it emits.
    datetime_filters_take_date_only: bool = False

    def _datetime_filter_keys(self) -> set[str]:
        """Dotted model paths of every filterable ``datetime`` field."""
        keys: set[str] = set()

        def walk(props: Any, prefix: str = "") -> None:
            if not isinstance(props, dict):
                return
            for name, spec in props.items():
                if not isinstance(spec, dict):
                    continue
                path = f"{prefix}.{name}" if prefix else name
                if spec.get("type") == "datetime" and spec.get("filterable"):
                    keys.add(path)
                sub = spec.get("properties") or (spec.get("node") or {}).get("properties")
                walk(sub, path)

        try:
            walk(self.fields())
        except Exception:  # noqa: BLE001 - a broken fields() must not break list
            return set()
        return keys

    def _strip_reference_filter_prefixes(
        self, params: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """Reference filter values arrive as speaking ids ("cus_20423"); the
        upstream filters on the bare numeric id, so strip the prefix for
        reference-typed filter keys (mirrors ``_ref_id`` on writes). Keyed off the
        MODEL filter key, so string/enum filters (number, tags, status, …) are
        left untouched. Also trims datetime filter values where the collection
        only accepts a date (see ``datetime_filters_take_date_only``). Runs before
        ``query_aliases`` rewrites the ``[key]``."""
        date_keys = self._datetime_filter_keys() if self.datetime_filters_take_date_only else set()
        ref_keys = self._reference_filter_keys()
        if not ref_keys and not date_keys:
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
                model_key = key_by_index.get(idx)
                if model_key in ref_keys and isinstance(v, str) and _SPEAKING_ID.match(v):
                    v = v.split("_", 1)[1]
                elif model_key in date_keys and isinstance(v, str) and "T" in v:
                    v = v.split("T", 1)[0]
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
        ours = False
        if not any(k == "include" for k, _ in params) and self.include:
            params.append(("include", self.include))
            ours = True
        url = f"{base_url.rstrip('/')}{path}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient, use: list[tuple[str, str]]) -> httpx.Response:
            return await c.get(url, params=use, headers=headers)

        async def _run(c: httpx.AsyncClient) -> httpx.Response:
            got = await _do(c, params)
            # Xentral rejects the WHOLE request when it does not know one of the
            # requested includes ("Requested include(s) `x` are not allowed"), so
            # an include this build lacks would take the entity down entirely
            # rather than cost it some detail. Drop ours and ask again; the
            # adapters treat a missing include key as "not loaded", not "empty".
            if got.status_code == 400 and ours and self._include_rejected(got):
                return await _do(c, [(k, v) for k, v in params if k != "include"])
            return got

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _run(c)
        else:
            resp = await _run(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    @staticmethod
    def _include_rejected(resp: httpx.Response) -> bool:
        """Is this 400 about the ``include`` parameter? Only then is retrying
        without it meaningful — any other 400 is the caller's own query."""
        try:
            text = (resp.content or b"").decode("utf-8", "ignore").lower()
        except (AttributeError, UnicodeDecodeError):
            return False
        return "include" in text

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
            # Complete a half-set money value HERE, before anything parses the body.
            # Subclasses read the body themselves to compose sub-resources
            # (Product's sale price is a salesPrices write, not a field), so
            # completing it further down inside _write_document leaves exactly those
            # composers blind to it — measured: a currency-only sale-price update
            # reached upstream as no price write at all.
            if method != "POST" and self.money_pairs:
                body = await self._complete_body_money_pairs(
                    handle, body, base_url, token, accept_language, client
                )
            return await self._write(
                method, handle, query, body, base_url, token, accept_language, client
            )
        # Consolidated `search` — fan out over the schema's `searchable` fields and
        # merge. Only intercepts when the entity declares such fields.
        #
        # This used to say the upstream has no cross-field search key and "would 400
        # as filter `search` not allowed". Both halves are wrong, checked against
        # schemas/openapi/documents.yml and measured on mvp:
        #   * v3 DOES have a native `?search=` on nine document endpoints
        #     (creditNotes, deliveryNotes, invoices, offers, productions,
        #     proformaInvoices, purchaseOrders, returnOrders, salesOrders), covering
        #     9-14 fields each — id, documentNumber, documentAddress.name/email/
        #     zipCode, customerNumber, customerOrderNumber, …
        #   * where it does NOT exist (customers, suppliers, products) the parameter
        #     is not refused but SILENTLY IGNORED: `?search=nonsense` on customers
        #     answers 200 with all 20128 rows.
        # So the fan-out is right for partners and products, and strictly worse than
        # the native call for documents: our document adapters search `number` alone,
        # and a sales-order search for the customer's name finds 0 where the native
        # one finds 14. Switching documents over is a separate change.
        #
        # Where the entity declares NO searchable fields the key is REFUSED by the
        # guard below rather than forwarded: advertising `searchFields` in the
        # metadata turned out not to be enough to keep callers away, and a
        # forwarded `search` is silently ignored upstream (see above), which a
        # caller reads as an unfiltered list being a search result.
        if handle is None:
            refusal = self.refuse_undeclared_filters(query)
            if refusal is not None:
                return refusal
            hit = extract_search(query)
            if hit is not None and self.native_search_fields:
                # Upstream searches this endpoint itself — hand the term over as the
                # top-level `?search=` it documents and let it do the OR server-side.
                # One request instead of N, and it covers what the fan-out cannot:
                # searching sales orders for the customer's name found 0 through the
                # emulation and 14 natively.
                value, _op = hit
                if value:
                    query = [*strip_search(query), ("search", value)]
            elif hit is not None and self.search_fields():
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
        # Sections this adapter fills from a sub-resource on the single read only.
        # A list leaves them null, and a null that means "not loaded" is
        # indistinguishable from one that means "not set" — a caller listing
        # products would conclude none of them has a sale price. The single read
        # already says which sections it could not reach; say it here too, for the
        # ones a list never even attempts.
        if self.detail_only_sections:
            extra["unavailableSections"] = list(self.detail_only_sections)
        return {"data": mapped, "meta": meta, "extra": extra}

    # ---- write orchestration --------------------------------------------
    def rejected_response(self, rejected: set[str]) -> AdapterResponse:
        """The 409 a write earns by naming fields the core will not write.

        A method rather than an inline body because adapters that compose a write
        outside the default path (salesOrder splits its line items off before
        delegating) must answer identically instead of dropping the rejection.

        The per-field ``reasons`` come from field-gaps.yaml. They matter because
        "not writable" covers two different things, and a blanket "read-only
        upstream" would be a false statement for the second: the upstream genuinely
        cannot do it, OR it could and we decline (a document number is always drawn
        from the number range, even though v3 would store a supplied one). A caller
        that only reads this response has to be able to tell those apart."""
        wishes = {
            w.get("field"): w.get("reason")
            for w in _field_gaps().get(self.manifest.key, [])
            if isinstance(w, dict) and w.get("reason")
        }
        body: dict[str, Any] = {
            "title": f"{self.manifest.key}: fields the core does not write",
            "detail": (
                "Either the upstream cannot write them today or the core declines them "
                "by decision (ADR-014: no overlay, no silent drop). See `reasons`, and "
                "field-gaps.yaml for the full backlog."
            ),
            "fields": sorted(rejected),
        }
        reasons = {f: wishes[f] for f in sorted(rejected) if f in wishes}
        if reasons:
            body["reasons"] = reasons
        return self._json(409, body)

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
        # upstream generations (Product); everyone else falls back to v3_path. A
        # delete can sit on a third one again, so it gets its own override first.
        base_path = self.v3_path
        if method.upper() == "DELETE" and self.delete_path:
            base_path = self.delete_path
        elif self.write_path:
            base_path = self.write_path
        path = base_path + (f"/{up_handle}" if up_handle else "")
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
            body = resp.json()
        except ValueError:
            body = {}
        # A create that answers 201 with an EMPTY body puts the new id in the
        # Location header (v1 warehouses, v2 products). Without this the write flow
        # has nothing to read back and the caller gets a success with no id.
        if resp.status_code < 400 and not (
            isinstance(body, dict) and (body.get("data") or {}).get("id")
        ):
            new_id = id_from_location(resp.headers.get("Location") or resp.headers.get("location"))
            if new_id:
                body = {"data": {"id": new_id}}
        return resp.status_code, body

    # ---- line items on the v3 sub-resource --------------------------------
    @staticmethod
    def _item_to_v3(i: dict[str, Any], currency: str = "EUR") -> dict[str, Any]:
        """One model line item → its v3 body. Adapters that set
        ``reconciles_line_items`` override this."""
        raise NotImplementedError

    def _item_props(self) -> dict[str, Any]:
        """The declared line-item properties — the allowlist unknown keys are held
        against. Read off ``fields()`` so it cannot drift from the schema."""
        items = self.fields().get("items") or {}
        return (items.get("node") or {}).get("properties") or {}

    def _line_items_url(self, base_url: str, up_id: str) -> str:
        return f"{base_url.rstrip('/')}{self.v3_path}/{up_id}/lineItems"

    async def _li_call(  # noqa: ANN001
        self, method, url, token, accept_language, client, payload=None
    ) -> tuple[int, Any]:
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001, ANN202
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

    async def _reconcile_line_items(  # noqa: ANN001
        self, handle, up_id, desired, base_url, token, accept_language, client
    ) -> dict[str, Any]:
        """Bring the document's line items to ``desired`` via the v3 lineItems
        sub-resource — the document PATCH cannot touch them. Contract (collection
        replace): an item WITH an existing ``id`` is PATCHed, one WITHOUT an id is
        POSTed (added), and an existing item OMITTED from ``desired`` is DELETEd.
        Returns per-op failures (empty = all ok) so a partial reconcile surfaces as
        a warning, never a silent drop."""
        base = self._line_items_url(base_url, str(up_id))
        # current line-item ids (skip text lines — they carry no product)
        st, payload = await self._get(
            base_url,
            token,
            handle=handle,
            query=[],
            accept_language=accept_language,
            client=client,
        )
        current_ids: list[str] = []
        # The document currency governs what a line's money may be sent in — read it
        # off the same fetch instead of defaulting the line items to EUR.
        doc_cur = "EUR"
        if st < 400 and isinstance(payload, dict):
            data = payload.get("data") or {}
            doc_cur = (data.get("financials") or {}).get("currency") or doc_cur
            for li in data.get("lineItems") or []:
                if isinstance(li, dict) and li.get("id") is not None and li.get("type") != "text":
                    current_ids.append(str(li["id"]))
        keep = {
            str(i["id"]) for i in desired if isinstance(i, dict) and i.get("id") not in (None, "")
        }
        failures: dict[str, list[Any]] = {}

        # DELETE the omitted lines first.
        for lid in current_ids:
            if lid not in keep:
                status, body = await self._li_call(
                    "DELETE", f"{base}/{lid}", token, accept_language, client
                )
                if status >= 400:
                    failures.setdefault("delete", []).append(
                        {"id": lid, "status": status, "error": body}
                    )
        # PATCH the kept-with-changes, POST the new ones.
        for it in desired:
            if not isinstance(it, dict):
                continue
            lid = it.get("id")
            v3 = self._item_to_v3(it, doc_cur)
            if lid not in (None, "") and str(lid) in current_ids:
                v3.pop("product", None)  # product is fixed on an existing line
                if not v3:
                    continue  # {id} only → keep unchanged, no-op
                status, resp = await self._li_call(
                    "PATCH", f"{base}/{lid}", token, accept_language, client, v3
                )
                if status >= 400:
                    failures.setdefault("update", []).append(
                        {"id": str(lid), "status": status, "error": resp}
                    )
            else:
                status, resp = await self._li_call("POST", base, token, accept_language, client, v3)
                if status >= 400:
                    failures.setdefault("add", []).append(
                        {"product": it.get("product"), "status": status, "error": resp}
                    )
        return failures

    async def _compose_item_write(  # noqa: ANN001
        self, method, handle, query, model, items, base_url, token, accept_language, client
    ):
        """UPDATE with ``items``: document-level fields still go through the normal
        PATCH, the line items are reconciled on the sub-resource. When items are the
        ONLY change the document PATCH is skipped so an empty body is never sent."""
        # This path splits `items` off before delegating, so map_write never sees
        # them — and with an items-only body it is not reached at all. Check the item
        # keys here, else an unsupported one is silently dropped on UPDATE.
        item_rejects = rejected_item_keys(items, self._item_props())
        if item_rejects:
            return self.rejected_response(item_rejects)

        rest = {k: v for k, v in model.items() if k != "items"}
        if rest:
            resp = await self._write_document(
                method,
                handle,
                query,
                json.dumps(rest).encode(),
                base_url,
                token,
                accept_language,
                client,
            )
            if resp.status_code >= 400:
                return resp
        up_id = handle.split("_", 1)[1] if handle and "_" in handle else handle
        failures = await self._reconcile_line_items(
            handle, str(up_id), items, base_url, token, accept_language, client
        )
        st, payload = await self._get(
            base_url,
            token,
            handle=handle,
            query=[],
            accept_language=accept_language,
            client=client,
        )
        if st >= 400:
            return self._json(
                st, payload if isinstance(payload, dict) else {"title": "read-back failed"}
            )
        data = self.map_read((payload.get("data") or {}) if isinstance(payload, dict) else {})
        if failures:
            data["_warnings"] = {"items": failures}
        return self._json(200, {"data": data})

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
        """Route an UPDATE carrying ``items`` to the line-item sub-resource when the
        adapter reconciles them; everything else writes the document directly.
        Create is unchanged either way — items ride the v3 create body."""
        if self.reconciles_line_items:
            try:
                model = json.loads(body or b"{}")
            except (ValueError, TypeError):
                model = {}
            items = model.get("items") if isinstance(model, dict) else None
            is_dry = any(k == "dryRun" and v in ("true", "1") for k, v in query)
            if method.upper() != "POST" and isinstance(items, list) and not is_dry:
                return await self._compose_item_write(
                    method, handle, query, model, items, base_url, token, accept_language, client
                )
        return await self._write_document(
            method, handle, query, body, base_url, token, accept_language, client
        )

    # Dotted model paths of money values the upstream treats as an ATOMIC pair —
    # it has no "set the currency" of its own, only "set amount+currency"
    # (PurchasePriceHandler: `pricePerUnit->has && priceCurrency->has && setPricePerUnit(…)`).
    # A currency-only update therefore has to be completed from the stored amount,
    # or it goes out as no price block at all: 2xx back, nothing changed, nothing
    # for the caller to see. Only for master data — a document POSITION has no
    # currency of its own (the header decides), so no document declares any.
    money_pairs: tuple[str, ...] = ()
    # Model sections this adapter can only fill from a sub-resource on the SINGLE
    # read (Product: prices.sale from /salesPrices, bom from /parts, …). A list
    # response names them in `extra.unavailableSections` so a null there is
    # readable as "not loaded here", never as "not set". Hydrating the list
    # instead would cost one upstream request per row.
    detail_only_sections: tuple[str, ...] = ()
    # The document has a printable form: `GET {v3_path}/{id}` answers with the
    # rendered PDF when asked for `Accept: application/pdf` (documented content
    # negotiation, scope <document>:read). Declaring it wires the generic
    # downloadPdf action below — there is nothing per-document about it.
    renders_pdf: bool = False

    # MODEL paths the UPSTREAM's own `?search=` covers on this endpoint. Non-empty
    # means the term is handed over natively instead of being emulated by a fan-out
    # over `search_fields()` — one request, and it reaches fields the emulation
    # cannot: a sales-order search for the customer's name found 0 through the
    # fan-out and 14 natively.
    #
    # Declare it ONLY where the upstream really searches. Where it does not, the
    # parameter is not refused but silently ignored — `/api/v3/customers?search=
    # nonsense` answers 200 with all 20128 rows — so a wrong entry here turns every
    # search into "return everything", which reads exactly like a working search.
    # The nine endpoints that have it are listed in schemas/openapi/documents.yml.
    #
    # The lists below are what the spec documents AND a live probe on mvp matched a
    # record by; upstream's own list is broader (it also covers `id`, `customerNumber`
    # and `internalDesignation`, which our model either does not expose as a path or
    # had no sample value for). Under-advertising is the safe direction.
    native_search_fields: tuple[str, ...] = ()

    async def _complete_body_money_pairs(  # noqa: ANN001
        self, handle, body, base_url, token, accept_language, client
    ) -> bytes | None:
        """Run the pair completion on the raw request body and hand back the body to
        use — the original bytes when there was nothing to complete."""
        try:
            model = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return body
        if not isinstance(model, dict):
            return body
        before = json.dumps(model, sort_keys=True)
        await self._complete_money_pairs(handle, model, base_url, token, accept_language, client)
        if json.dumps(model, sort_keys=True) == before:
            return body
        return json.dumps(model).encode()

    async def _complete_money_pairs(  # noqa: ANN001
        self, handle, model, base_url, token, accept_language, client
    ) -> None:
        """Fill in the amount for every declared money pair the caller set the
        currency of but not the amount. Reads the record once, and only when such a
        half-set pair is actually present — an ordinary write costs nothing."""
        pending = [
            path
            for path in self.money_pairs
            if isinstance((money := self._value_at(model, path)), dict)
            and money.get("currency") is not None
            and money.get("amount") is None
        ]
        if not pending or not handle:
            return
        # The adapter's OWN single read, not _get + map_read: a money value can live
        # in a section the adapter hydrates from a sub-resource (Product.prices.sale
        # comes from /salesPrices), and map_read alone does not know about those.
        # Reading the short way found no stored amount and the currency-only write
        # went out incomplete again — the exact bug this method exists to prevent.
        resp = await self.request(
            method="GET",
            handle=handle,
            query=[],
            body=None,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if resp.status_code >= 400:
            return  # unreadable → leave the body alone and let upstream answer
        try:
            current = json.loads(resp.content or b"{}").get("data") or {}
        except (ValueError, TypeError):
            return
        for path in pending:
            stored = self._value_at(current, path)
            amount = stored.get("amount") if isinstance(stored, dict) else None
            if amount is not None:
                self._value_at(model, path)["amount"] = amount

    @staticmethod
    def _value_at(record: Any, path: str) -> Any:
        node = record
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    def _created_handle(self, resp: Any) -> Any:
        """Upstream handle of a just-created record, used for the read-back.

        Defaults to the ``id`` of the create response. Adapters whose detail read
        addresses records by something else override this — the entity API reads
        by ``uuid`` and does not even allow filtering on ``id``, so re-reading a
        newly created record by its id answers 404 there.
        """
        rec = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(rec, dict):
            rec = resp if isinstance(resp, dict) else {}
        return rec.get("id")

    async def _write_document(
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
        new_id = up_handle or self._created_handle(resp)
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
            if not isinstance(rec, dict):
                # Some v1 collections have NO detail endpoint — `GET /v1/warehouses/1`
                # answers 404 while the record is perfectly readable through a list
                # filtered by id. Upstream says so itself: the Location header on a
                # create is that very query, not a link. Without this fallback the
                # create answers with the bare id it got from the header, and a
                # caller (or the capability probe) reads "sent but did not persist".
                _, lpayload = await self._get(
                    base_url,
                    token,
                    handle=None,
                    query=[
                        ("page[number]", "1"),
                        ("page[size]", "5"),
                        ("filter[0][key]", "id"),
                        ("filter[0][op]", "equals"),
                        ("filter[0][value]", str(new_id)),
                    ],
                    accept_language=accept_language,
                    client=client,
                )
                rows = lpayload.get("data") if isinstance(lpayload, dict) else None
                if isinstance(rows, list):
                    rec = next(
                        (
                            r
                            for r in rows
                            if isinstance(r, dict) and str(r.get("id")) == str(new_id)
                        ),
                        None,
                    )
            if isinstance(rec, dict):
                return self._json(201 if method == "POST" else 200, {"data": self.map_read(rec)})
        # Read-back impossible: answer the record the WRITE returned, still mapped.
        # Returning `resp` verbatim would let a create answer a raw upstream record
        # while every read answers the model — the same id under two shapes.
        written = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(written, dict) and written:
            return self._json(201 if method == "POST" else 200, {"data": self.map_read(written)})
        return self._json(201 if method == "POST" else 200, resp if isinstance(resp, dict) else {})

    # A rendered document can be large, and the caller's transport is JSON. This is
    # the point where a 20 MB attachment would turn into a 27 MB base64 string in
    # someone's context window, so it is refused with a readable reason instead.
    _PDF_MAX_BYTES = 8 * 1024 * 1024

    async def _download_pdf(  # noqa: ANN001
        self, ids, base_url, token, accept_language, client
    ) -> AdapterResponse:
        """Fetch the document's PDF and return it as a file payload.

        Upstream renders it on the record itself — no separate endpoint, no files
        sub-resource: `GET {v3_path}/{id}` with `Accept: application/pdf`. Note it
        serves the ARCHIVED copy when one exists (written on send and on write
        protection, NOT on release) and renders fresh otherwise, so two calls can
        legitimately differ after a document is sent.

        The bytes leave here as base64 inside JSON because the gateway decodes any
        non-JSON body as text — raw PDF bytes would arrive as replacement
        characters. Handing the payload to a file store is the caller's job.
        """
        if not ids:
            return self._refuse(422, "downloadPdf needs a record id")
        up_id = str(ids[0]).split("_", 1)[1] if "_" in str(ids[0]) else str(ids[0])
        url = f"{base_url.rstrip('/')}{self.v3_path}/{up_id}"
        headers = {**self._headers(token, accept_language), "Accept": "application/pdf"}
        try:
            if client is not None:
                resp = await client.request("GET", url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=60) as c:
                    resp = await c.request("GET", url, headers=headers)
        except httpx.HTTPError as exc:
            return self._json(502, {"title": f"PDF request failed: {exc}"})
        if resp.status_code >= 400:
            return AdapterResponse(
                resp.status_code, resp.content, {"content-type": "application/json"}
            )
        content = resp.content or b""
        # Content negotiation that silently fell back to JSON would otherwise be
        # handed on as a "PDF" the caller cannot open.
        if not content.startswith(b"%PDF"):
            return self._json(
                502,
                {
                    "title": "upstream did not answer with a PDF",
                    "detail": (
                        f"content-type {resp.headers.get('content-type', '?')}; "
                        f"first bytes {content[:16]!r}"
                    ),
                },
            )
        if len(content) > self._PDF_MAX_BYTES:
            return self._json(
                413,
                {
                    "title": "document PDF is too large to return inline",
                    "detail": (
                        f"{len(content)} bytes exceeds the {self._PDF_MAX_BYTES} byte "
                        "limit for a base64 payload"
                    ),
                },
            )
        number = None
        st, rec = await self._get(
            base_url, token, handle=up_id, query=[], accept_language=accept_language, client=client
        )
        if st < 400 and isinstance(rec, dict):
            number = (rec.get("data") or {}).get("documentNumber")
        # Prefixed with the entity: document numbers are only unique per type, and
        # a quote and a purchase order on mvp both answer to 100000. Two files
        # called 100000.pdf in one store are one file.
        stem = f"{self.manifest.key}-{number or up_id}".replace("/", "-")
        return self._json(
            200,
            {
                "data": {"id": str(ids[0]), "documentNumber": number},
                "result": {
                    "file": {
                        "filename": f"{stem}.pdf",
                        "contentType": "application/pdf",
                        "sizeBytes": len(content),
                        "contentBase64": base64.b64encode(content).decode("ascii"),
                    }
                },
            },
        )

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
            return self._refuse(422, f"{action_key} requires a non-empty 'title'.")
        if not ids:
            return self._refuse(422, f"{action_key} needs a target id (ids[])")
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
        if action_key == "downloadPdf" and self.renders_pdf:
            return await self._download_pdf(ids, base_url, token, accept_language, client)
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
            return self._refuse(422, f"{action_key} needs a target id (ids[])")
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
# Raw upstream status values a map did not know, together with the model value that
# was reported instead. A miss is silent by construction — the default takes over —
# so unless it is recorded here, nobody learns that `sent` became `draft`. Measured
# consequence before this existed: 44 dispatched delivery notes on mvp read as
# drafts, and every `done` return read as still requested. The verify run reads and
# clears this per entity; nothing else consumes it.
STATUS_FALLBACKS: set[tuple[str, str]] = set()


def custom_fields_to_v3(value: Any) -> list[dict[str, Any]] | None:
    """Model free-field rows → the v3 ``customFields`` body.

    ``key`` and ``value`` come from the caller; ``label`` is required upstream, so a
    row without one is refused here rather than sent to earn a 400 the caller
    cannot read. ``type`` is upstream's and never echoed back. None = the whole
    value is unusable.

    Shared by Customer and Supplier: upstream gives both the same
    ``OutputCustomFields`` contract, so the mapping must not drift between them.
    """
    if not isinstance(value, list):
        return None
    out: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            return None
        key, label = row.get("key"), row.get("label")
        if not key or not label:
            return None
        out.append({"key": key, "label": label, "value": row.get("value")})
    return out


def status_map(mapping: dict[str, str], value: Any, default: str | None = None) -> str | None:
    if value in (None, ""):
        return default
    key = str(value)
    if key in mapping:
        return mapping[key]
    if default is None:
        return key  # no default to hide behind — the raw value passes through
    STATUS_FALLBACKS.add((key, default))
    return default


# A read-only computed marker used a lot in the model (totals, holds, documents…).
RO: dict[str, Any] = {"access": "readOnly"}

# A field the record is meaningless without — a sales order with no customer, a
# product with no name. Spelled as a Laravel validation rule because that is the
# dialect native Xentral emits and every consumer already parses it (the
# workspace form renders the marker, MCP `describe` reports it to an agent).
#
# BUSINESS necessity first, but every entry here is also a field the create path
# genuinely cannot do without — the set was probed against a live tenant (mvp)
# rather than reasoned about, because reasoning got it wrong once already:
# "a document position without a product is a legitimate free-text line" sounded
# obvious and is false. All seven customer/supplier documents answer
# `400 lineItems.0.product: product is required`. Only PurchaseInvoice accepts a
# free-text line (201, `product: null`), which is why it alone is unmarked.
#
# Necessity that is *conditional* stays unmarked, and StockMovement.quantity is
# the reason the rule exists: it is required WITHOUT `setQuantityTo` and rejected
# WITH it ("correction takes quantity (delta) OR setQuantityTo, not both", see
# stock_movement's own validator). A flat `required` would contradict the core.
REQUIRED: dict[str, Any] = {"rules": ["required"]}

# Callable type alias for adapters that build sub-trees.
FieldBuilder = Callable[[], dict[str, dict[str, Any]]]


# --- goods receipts ---------------------------------------------------------
# Two v1 endpoints receive goods and BOOK them in one call:
#   POST /api/v1/purchaseOrders/{id}/goodsReceipts   (line key: purchaseOrderPosition)
#   POST /api/v1/returns/{id}/goodsReceipts          (line key: returnPosition)
# They differ only in that key, so the model→wire translation lives here once
# rather than being copied into both adapters and drifting.
#
# The model's vocabulary is deliberately NOT v1's: `items` because every document
# in this core has items, and `putaways` because booking stock onto a location is
# what StorageLocation.putaway is called — one concept, one word. Upstream's
# nested `qualityControlAttributes` is flat here (`batch`, `bestBefore`,
# `serialNumbers`), because the model has batches and serial numbers, not a
# quality-control concept. (v1's returns variant also accepts a QC sub-quantity;
# it is not exposed — its semantics next to the movement's own quantity are not
# documented, and a field nobody can explain is worse than a missing one.)


def goods_receipt_command(line_field: str, line_label: str) -> dict[str, Any]:
    """The `command` schema for a receive-and-book action.

    ``line_field`` is the MODEL name for the document line the receipt books
    against — ``orderItem`` on a purchase order (matching DeliveryNote and
    SalesInvoice, which already call the originating line that), ``returnItem`` on
    a return. Same slot, entity-appropriate word.
    """
    return {
        "type": "object",
        "required": ["date", "items"],
        "properties": {
            "date": {
                "type": "string",
                "label": "Posting date (YYYY-MM-DD)",
                "description": (
                    "Required, although the OpenAPI spec marks it optional — measured: v1 "
                    "answers 400 without it. Not defaulted here on purpose: the posting "
                    "date of a stock booking is a decision, not a convenience."
                ),
            },
            "items": {
                "type": "array",
                "label": "Received items",
                "items": {
                    "type": "object",
                    "required": ["product", "quantity"],
                    "properties": {
                        "product": {"type": "string", "label": "Product id (prd_…)"},
                        "quantity": {"type": "number", "label": "Received quantity"},
                        line_field: {"type": "string", "label": line_label},
                        "putaways": {
                            "type": "array",
                            "label": "Where the quantity is stored",
                            "items": {
                                "type": "object",
                                "required": ["quantity"],
                                "properties": {
                                    "quantity": {"type": "number", "label": "Quantity"},
                                    "warehouse": {"type": "string", "label": "Warehouse (wh_…)"},
                                    "storageLocation": {
                                        "type": "string",
                                        "label": "Storage location (loc_…)",
                                    },
                                    "batch": {"type": "string", "label": "Batch / lot"},
                                    "bestBefore": {"type": "string", "label": "Best-before date"},
                                    "serialNumbers": {
                                        "type": "array",
                                        "label": "Serial numbers",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def goods_receipt_payload(
    command: dict[str, Any], *, line_field: str, line_key: str, ref_id
) -> tuple[dict[str, Any] | None, str | None]:
    """Model command → v1 body. Returns ``(payload, error_message)``.

    Validation happens before the call rather than after: a stock booking is not
    the place to let a malformed payload through and read the upstream's error.
    """
    if not command.get("date"):
        return None, (
            "needs command.date (YYYY-MM-DD) — upstream rejects a receipt without a posting date"
        )
    items = command.get("items")
    if not isinstance(items, list) or not items:
        return None, (f"needs command.items=[{{product, quantity, {line_field}?, putaways?}}]")

    positions: list[dict[str, Any]] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        product = ref_id(m.get("product"))
        if product is None:
            return None, f"missing product in {m}"
        try:
            qty = float(m.get("quantity"))
        except (TypeError, ValueError):
            return None, f"bad quantity in {m}"
        if qty <= 0:
            return None, f"quantity must be > 0 in {m}"
        pos: dict[str, Any] = {"product": product, "quantity": qty}
        line = ref_id(m.get(line_field))
        if line is not None:
            pos[line_key] = line

        movements: list[dict[str, Any]] = []
        for p in m.get("putaways") or []:
            if not isinstance(p, dict):
                continue
            try:
                pqty = float(p.get("quantity", qty))
            except (TypeError, ValueError):
                return None, f"bad putaway quantity in {p}"
            mv: dict[str, Any] = {"quantity": pqty}
            for key in ("warehouse", "storageLocation"):
                target = ref_id(p.get(key))
                if target is not None:
                    mv[key] = target
            qc: dict[str, Any] = {}
            if p.get("batch") is not None:
                qc["batch"] = p["batch"]
            if p.get("bestBefore") is not None:
                qc["bestBeforeDate"] = p["bestBefore"]
            serials = [s for s in (p.get("serialNumbers") or []) if s]
            if serials:
                qc["serialNumbers"] = [
                    {"number": s.get("number") if isinstance(s, dict) else str(s)} for s in serials
                ]
            if qc:
                mv["qualityControlAttributes"] = qc
            movements.append(mv)
        if movements:
            pos["stockMovements"] = movements
        positions.append(pos)

    return {"date": command["date"], "positions": positions}, None
