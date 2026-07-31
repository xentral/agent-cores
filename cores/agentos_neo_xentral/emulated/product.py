"""Xentral V3 facade · product — Stammdaten (docs/01-model.md §6.3, ADR-011).

Reads Xentral v3 ``/api/v3/products`` (PR #24325, ``product:read``). At the time
this adapter was written the PR was **not yet deployed to mvp** (404) — the field
names below are grounded in the PR's own ``ProductResource`` (same source the
already-built ``xentral_api`` Product adapter uses, see that file's
``_root_properties``), not guessed. Re-run ``checks/verify.py`` once the endpoint
is live to flip this entity's cells from grey to green.

**Flag → enum consolidation (ADR-011)** — the v3 resource still exposes ~10
mutually-exclusive booleans; this facade folds them into the new model's small
enums so a consumer reads one field instead of guessing a boolean combination:
  - ``kind``: isShippingCostsProduct → shippingCost; isFee → fee;
    isServiceProduct/isService → service; else physical. (No upstream signal
    for "digital" yet — tracked as a wish.)
  - ``status``/``statusReason``: isDeleted → archived; isDisabled → inactive
    (+ disabledReason); else active.
  - ``production.mode``: isAssembledJustInTime → justInTime;
    isExternallyProduced → external; isProductionProduct → inHouse; else none.

v3's own pagination differs from every other v3 endpoint in this core
(``page``/``perPage`` instead of ``page[number]``/``page[size]``, no ``include``
default) — handled in ``_get`` override below, same translation the xentral_api
adapter performs.

The v3 products endpoint is **read-only by design** (per the PR). Write (create/
update) therefore goes through **v2 products** (``POST/PATCH /api/v2/products`` —
``product.createV2`` / ``product.updateV2``), targeted via ``write_path`` while
reads stay on v3. ``map_write`` below maps the new model onto the v2 create body
(field names grounded in the v2 OpenAPI request schema). Free-field VALUES ride the
v2 body as ``freeFields[{id, value}]``; fields the v2 body does not accept (packaging
units, stock) stay read-only and surface as blue wishes (ADR-014).

**Sale price**, the **bill of materials** and the **property values** are not part
of the product body upstream — each is a separate resource that ``_write`` composes
on top of a successful create/update: the sale price via ``POST /api/v3/salesPrices``
(v1 fallback), the BOM via the product's ``/parts`` sub-resource (POST v2 the desired
parts, then DELETE v1 the previously existing lines — SET semantics, non-destructive
on a failed POST), the property values via ``PATCH /api/v1/products/{id}/properties``
(upsert). A composition failure after a successful product write is reported honestly
as a partial success (never a false 201, never a silent drop).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, map_tags, money, prop, ref, tags_prop

# ---- detail hydration (issue #23) -----------------------------------------
# `describe` advertises stock, bom and prices.sale, but the v3 product payload
# carries none of them, so map_read could only ever emit empty ones. Each has
# its own v1 sub-resource keyed by product id; a single-record read fetches all
# three concurrently. List reads deliberately do NOT — a 25-row page would cost
# 75 extra round trips, and lists are the hot path. That summary-vs-detail
# asymmetry is the usual trade; `get` is where a product is actually inspected.
#
# customFields (free-field VALUES) come from the v3 ``include=customFields`` payload
# (map_custom_fields) — NOT a hydration round trip. Properties (Eigenschaften) DO
# hydrate here, from the v1 /properties sub-resource (map_properties).
_SUB_STOCKS = "stocks"
_SUB_PARTS = "parts"
_SUB_SALES_PRICES = "salesPrices"
_SUB_PROPERTIES = "properties"


def _num(value: Any) -> Any:
    """Upstream sends stock counts as floats; emit ints when they are whole."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return int(value) if float(value).is_integer() else value


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _valid_on(row: dict[str, Any], today: date) -> bool:
    start, end = _day(row.get("validFrom")), _day(row.get("expiresAt"))
    if start and today < start:
        return False
    return not (end and today > end)


# The stock block's keys, in emit order. Shared so the "no stock data" fallback
# in map_read cannot drift out of sync with what map_stock produces — a caller
# that sees a key on one product and not on another cannot tell absence from
# "this product has none".
_STOCK_KEYS = (
    "available",
    "physical",
    "reserved",
    "openSalesOrders",
    "producible",
    "correction",
    "calculated",
    "pseudo",
    "incoming",
    "belowMinimum",
)


def map_stock(payload: Any, minimum: Any) -> dict[str, Any] | None:
    """``/products/{id}/stocks`` totals → the model's stock block.

    The endpoint computes EIGHT figures and this used to surface two of them.
    ``available`` (= upstream ``sellable``) is the right number for "may I sell
    this", but it is NOT what lies on the shelf, and a reader had no way to see
    the difference — nor any of the other figures the same call already carried.
    They are all mapped now, under names that cannot be confused:

      physical         what is actually on the shelf
      available        sellable — what may still be sold
      reserved         committed to orders
      openSalesOrders  demanded by open orders, not yet shipped
      producible       how many could be built from components on hand
      correction       manual up/down adjustment applied before publishing
      calculated       the figure after that correction
      pseudo           a formula-driven stand-in figure; null when unconfigured

    ``correction``/``calculated``/``pseudo`` are publication mechanics, not
    physical facts — Xentral documents them only for the sales-channel block,
    so their field descriptions say what they are and their arithmetic is left
    unasserted rather than guessed.

    ``incoming`` still has no counterpart upstream, so it stays None instead of
    borrowing an approximate field. ``belowMinimum`` is derived against the
    product's own minimum stock, and is None when either side is unknown rather
    than defaulting to False.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    totals = data.get("totals") if isinstance(data, dict) else None
    if not isinstance(totals, dict):
        return None
    available = totals.get("sellable")
    below = None
    if isinstance(available, (int, float)) and isinstance(minimum, (int, float)):
        below = bool(available < minimum)
    return {
        "available": _num(available),
        "physical": _num(totals.get("physical")),
        "reserved": _num(totals.get("reserved")),
        "openSalesOrders": _num(totals.get("openSalesOrders")),
        "producible": _num(totals.get("producible")),
        "correction": _num(totals.get("correction")),
        "calculated": _num(totals.get("calculated")),
        "pseudo": _num(totals.get("pseudo")),
        "incoming": None,
        "belowMinimum": below,
    }


def map_bom_items(payload: Any) -> list[dict[str, Any]]:
    """``/products/{id}/parts`` rows → ``bom.items``. One level only.

    Each part is itself a product, so a full explosion means recursing per
    child. That is a per-node round trip with no upstream roll-up, so the
    facade emits the direct children and leaves recursion to the caller.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    items: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        p = r.get("product") if isinstance(r.get("product"), dict) else {}
        quantity = r.get("amount")
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            pass
        items.append(
            {
                "product": ref("prd_", p.get("id"), p.get("number"), p.get("name"), "products"),
                "quantity": _num(quantity),
                "type": r.get("type"),
                "reference": r.get("reference") or None,
            }
        )
    return items


def map_custom_fields(raw: Any) -> list[dict[str, Any]]:
    """v3 ``include=customFields`` rows → the model's customFields collection. The v3
    ``key`` is ``customField<N>`` (N = 1..40, the free-field slot); the model exposes
    it as an integer ``number`` so a write can target the v2 ``freeFields[{id}]`` slot.
    """
    out: list[dict[str, Any]] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        key = str(r.get("key") or "")
        number: int | None = None
        if key.startswith("customField"):
            try:
                number = int(key[len("customField") :])
            except ValueError:
                number = None
        out.append({"number": number, "label": r.get("label"), "value": r.get("value")})
    return out


def map_properties(payload: Any) -> list[dict[str, Any]]:
    """``/products/{id}/properties`` rows → the model's properties collection. Each
    row is one assigned property value: ``{property, name, value, unit}``."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        pdef = r.get("property") if isinstance(r.get("property"), dict) else {}
        out.append(
            {
                "property": ref(
                    "pprop_", pdef.get("id"), None, pdef.get("name"), "productsProperties"
                ),
                "name": pdef.get("name"),
                "value": r.get("value"),
                "unit": r.get("unit"),
            }
        )
    return out


def pick_sale_price(payload: Any, today: date) -> dict[str, Any] | None:
    """The product's own list price from ``/products/{id}/salesPrices``.

    "The" sale price is the unscoped base tier: no customer and no customer
    group (those are negotiated prices, not the product's), the lowest quantity
    threshold, and valid today. An entirely expired scale yields None rather
    than a stale number — mvp's prd_1 carries twenty tiers that all lapsed in
    2023, and reporting 1.00 EUR for it would be worse than reporting nothing.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    best_qty: float | None = None
    best: dict[str, Any] | None = None
    for r in rows or []:
        if not isinstance(r, dict) or r.get("customer") or r.get("customerGroup"):
            continue
        if not _valid_on(r, today):
            continue
        try:
            qty = float(r.get("amount"))
        except (TypeError, ValueError):
            qty = 1.0
        if best_qty is None or qty < best_qty:
            best_qty, best = qty, r
    if best is None:
        return None
    price = best.get("price") if isinstance(best.get("price"), dict) else {}
    return money(price.get("amount"), price.get("currency") or "EUR")


_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("active", "inactive", "archived")
]
_KIND_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("physical", "service", "digital", "shippingCost", "fee")
]
_PRODUCTION_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("none", "inHouse", "external", "justInTime")
]

# Field-flag shorthands (mirror customer.py's _CU): created+updated vs create-only.
_CU: dict[str, Any] = {"creatable": True, "updatable": True}
_C: dict[str, Any] = {"creatable": True}

# v2 products REQUIRES a project on create; the model's project is optional, so an
# unset project falls back to the standard project (id 1 in every Xentral tenant).
_DEFAULT_PROJECT_ID = "1"

# Sale price is written to a separate resource. Primary is v3 salesPrices (aligned
# with the model); v1 salesPrices is the documented fallback (same field shape).
_SALES_PRICE_PATH = "/api/v3/salesPrices"
_SALES_PRICE_PATH_FALLBACK = "/api/v1/salesPrices"

# v3 products cannot filter by manufacturer number, but v2 products can. A
# manufacturerNumber filter is resolved here (ids) then read back via v3 — this is
# the key an agent needs to match a supplier price list to products (MPN).
_V2_PRODUCTS = "/api/v2/products"
_MPN_MODEL_KEY = "identifiers.manufacturerNumber"
_MPN_V2_KEY = "manufacturerNumber"

# BOM parts are a separate sub-resource keyed by the parent product id. Writes go to
# v2 (POST additive, PATCH by part-line id); DELETE is v1 (by part-line id). Reads
# use v1 list (map_bom_items). Composed in _write like the sale price.
_PARTS_V2 = "/api/v2/products/{id}/parts"
_PARTS_V1 = "/api/v1/products/{id}/parts"
# Product properties (Eigenschaften): v1 GET lists {property, value, unit} rows,
# PATCH upserts them. Read hydrates on `get`; write composes in _write.
_PROPERTIES_V1 = "/api/v1/products/{id}/properties"
# The v2 parts `type` enum (the model passes it straight through; default on create).
_PART_TYPES = ("shopping part", "information part / service", "provision")

# model tax.rate → v2 salesTax enum (model 'exempt' is upstream 'free').
_TAX_TO_V2 = {"standard": "standard", "reduced": "reduced", "exempt": "free"}
# upstream taxRate/salesTax → model tax.rate (the reverse; unknowns pass through).
_TAX_FROM_UPSTREAM = {"standard": "standard", "reduced": "reduced", "free": "exempt"}
# model tracking.serialNumbers → v2 serialNumbersMode enum. The model read passes
# the upstream value straight through, so canonical modes round-trip; 'none' (the
# model's "off") maps to the v2 'disabled'.
_SN_MODES = {"disabled", "user", "product", "productAndWarehouse"}


def _kind(r: dict[str, Any]) -> str:
    if r.get("isShippingCostsProduct"):
        return "shippingCost"
    if r.get("isFee"):
        return "fee"
    if r.get("isServiceProduct") or r.get("isService"):
        return "service"
    return "physical"


def _status(r: dict[str, Any]) -> str:
    if r.get("isDeleted"):
        return "archived"
    if r.get("isDisabled"):
        return "inactive"
    return "active"


def _production_mode(r: dict[str, Any]) -> str:
    if r.get("isAssembledJustInTime"):
        return "justInTime"
    if r.get("isExternallyProduced"):
        return "external"
    if r.get("isProductionProduct"):
        return "inHouse"
    return "none"


def _tax_rate(r: dict[str, Any]) -> str | None:
    # v3 read exposes the rate as ``taxRate``; the v2 view calls it ``salesTax``.
    # Upstream "free" is the model's "exempt"; unknown values pass through.
    v = r.get("taxRate") or r.get("salesTax")
    return _TAX_FROM_UPSTREAM.get(v, v)


class ProductAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Product",
        label_en="Product",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.product",
        source_apis=("agentos_neo_xentral",),
        description=(
            "Product master data, including the single standard sale (list) price "
            "via prices.sale. Customer-specific, customer-group and scale (Staffel) "
            "prices are NOT set here — use the PriceList entity for those."
        ),
        # Read via v3 (PR #24325, read-only by design); WRITE via v2 products
        # (POST/PATCH /api/v2/products — see write_path). Sale price is a separate
        # resource composed on top of the create (see _write).
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/products"
    # v3 products is read-only; create/update go to v2 products (product.createV2 /
    # product.updateV2). The base's _send targets this for POST/PATCH; reads stay v3.
    write_path = "/api/v2/products"
    # customFields carries the per-product free-field VALUES in the v3 payload (works
    # on list AND single read) — cheaper than a hydration round trip.
    include = "project,defaultSupplier,merchandiseGroup,tags,customFields"
    # A `status` filter maps to the v3 `isDisabled` flag: the default list is
    # active-only (upstream hides disabled), so this lets a caller find inactive
    # products explicitly (records filters=[{key:"status", value:"inactive"}]).
    # value → isDisabled bool; key → isDisabled. (archived is isDeleted, not
    # covered by this single-key filter.)
    # MODEL path → upstream filter key. The upstream reports its own allowed set
    # in the 400 it answers to an unknown filter: id, number, ean, name,
    # manufacturerProductNumber, project.id, isVariant, isMatrixProduct,
    # updatedAt, isDisabled, isDeleted. Everything in that list that the model
    # can express is declared filterable; `isVariant` is not, because the model
    # has no boolean for it (variant.of is the parent reference, a different
    # question) — adding one would be a field, not a filter.
    query_aliases = {
        "status": "isDisabled",
        "project": "project.id",
        "variant.isMatrix": "isMatrixProduct",
        "identifiers.ean": "ean",
        "identifiers.manufacturerNumber": "manufacturerProductNumber",
    }
    filter_value_maps = {"status": {"active": "false", "inactive": "true"}}
    preview_template = "{{name}}"
    sections = {
        "general": {"label": "General"},
        "identifiers": {"label": "Identifiers"},
        "prices": {"label": "Prices"},
        "tax": {"label": "Tax"},
        "logistics": {"label": "Logistics"},
        "tracking": {"label": "Tracking"},
        "stock": {"label": "Stock"},
        "production": {"label": "Production"},
        "variant": {"label": "Variant"},
    }

    # v2 products create/update answers 201/204 with an EMPTY body and puts the
    # new id in the ``Location`` header (``…/api/v2/products/{id}``) — unlike the
    # v3 writes the base assumes, which echo ``{"data":{"id":…}}``. Override _send
    # to surface that id as a synthetic body so the base write flow (read-back,
    # sale-price composition) can find it. Mirrors base._send but keeps headers.
    async def _send(  # noqa: ANN001
        self, base_url, token, method, up_handle, payload, accept_language, client
    ):
        path = (self.write_path or self.v3_path) + (f"/{up_handle}" if up_handle else "")
        url = f"{base_url.rstrip('/')}{path}"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=60.0) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code < 400 and not (
            isinstance(body, dict) and (body.get("data") or {}).get("id")
        ):
            loc = resp.headers.get("Location") or resp.headers.get("location")
            if loc:
                new_id = loc.rstrip("/").rsplit("/", 1)[-1]
                if new_id:
                    body = {"data": {"id": new_id}}
        return resp.status_code, body

    # v3 products uses page/perPage (not page[number]/page[size]) and needs no
    # default include (unlike the documents) — same quirk as xentral_api's
    # Product adapter. Override just the pagination translation.
    async def _get(self, base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001
        translated: list[tuple[str, str]] = []
        has_per_page = False
        for key, value in query:
            if key == "page[size]":
                translated.append(("perPage", value))
                has_per_page = True
                continue  # noqa: E702
            if key == "page[number]":
                translated.append(("page", value))
                continue  # noqa: E702
            translated.append((key, value))
        if not has_per_page:
            translated.append(("perPage", "50"))
        return await super()._get(
            base_url,
            token,
            handle=handle,
            query=translated,
            accept_language=accept_language,
            client=client,
        )

    # v3 products rejects a `tags` filter outright (not in its filter
    # allow-list), while every other tag entity filters natively. The model
    # promises tag filtering wherever tags exist, so the facade emulates it
    # here: scan the upstream list (capped) and match on mapped tag titles.
    _TAG_SCAN_PAGE_SIZE = 50
    _TAG_SCAN_MAX_PAGES = 20
    _V2_SCAN_PAGE_SIZE = 50
    _V2_SCAN_MAX_PAGES = 20

    async def request(  # noqa: ANN001
        self,
        *,
        method,
        handle,
        query,
        body,
        base_url,
        token,
        accept_language=None,
        client=None,
    ):
        if method.upper() == "GET" and not handle:
            terms, rest, page, size = self._split_tags_filter(query)
            if terms:
                return await self._list_by_tags(
                    terms, rest, page, size, base_url, token, accept_language, client
                )
            mpns, page, size = self._split_mpn_filter(query)
            if mpns is not None:
                return await self._list_by_manufacturer_number(
                    mpns, page, size, base_url, token, accept_language, client
                )
        resp = await super().request(
            method=method,
            handle=handle,
            query=query,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if method.upper() == "GET" and handle:
            return await self._hydrate_detail(resp, base_url, token, accept_language, client)
        return resp

    async def _fetch_sub(  # noqa: ANN001
        self, suffix, up_id, base_url, token, accept_language, client
    ):
        """GET one v1 product sub-resource. None on any failure.

        Sub-resources are additive detail, so a failing one must not take the
        product read down with it — the caller reports which sections could not
        be loaded instead of guessing an empty value for them.
        """
        url = f"{base_url.rstrip('/')}/api/v1/products/{up_id}/{suffix}"
        try:
            resp = await client.get(url, headers=self._headers(token, accept_language))
            if resp.status_code >= 400:
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def _hydrate_detail(  # noqa: ANN001
        self, resp, base_url, token, accept_language, client
    ):
        if resp.status_code != 200:
            return resp
        try:
            body = json.loads(resp.content or b"{}")
        except (ValueError, TypeError):
            return resp
        rec = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rec, dict):
            return resp
        up_id = str(rec.get("id") or "").removeprefix("prd_")
        if not up_id:
            return resp

        async def _gather(c):  # noqa: ANN001, ANN202
            return await asyncio.gather(
                self._fetch_sub(_SUB_STOCKS, up_id, base_url, token, accept_language, c),
                self._fetch_sub(_SUB_PARTS, up_id, base_url, token, accept_language, c),
                self._fetch_sub(_SUB_SALES_PRICES, up_id, base_url, token, accept_language, c),
                self._fetch_sub(_SUB_PROPERTIES, up_id, base_url, token, accept_language, c),
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                stocks, parts, prices, properties = await _gather(c)
        else:
            stocks, parts, prices, properties = await _gather(client)

        unavailable: list[str] = []
        if stocks is None:
            unavailable.append("stock")
        else:
            minimum = (rec.get("logistics") or {}).get("minimumStockQuantity")
            mapped = map_stock(stocks, minimum)
            if mapped is not None:
                rec["stock"] = mapped
        if parts is None:
            unavailable.append("bom")
        else:
            rec["bom"] = {"items": map_bom_items(parts)}
        if prices is None:
            unavailable.append("prices.sale")
        else:
            rec.setdefault("prices", {})["sale"] = pick_sale_price(prices, date.today())
        if properties is None:
            unavailable.append("properties")
        else:
            rec["properties"] = map_properties(properties)

        out: dict[str, Any] = {"data": rec}
        if unavailable:
            # An empty section and an unreachable one are not the same answer.
            out["extra"] = {"unavailableSections": unavailable}
        return self._json(200, out)

    @staticmethod
    def _split_tags_filter(
        query: list[tuple[str, str]],
    ) -> tuple[list[str], list[tuple[str, str]], int, int]:
        """Split ``filter[i][…]`` triplets targeting ``tags`` out of the query.

        Returns (tag terms, remaining query with filters reindexed, page, size).
        With no tags filter present the original query is returned untouched.
        """
        triplets: dict[str, dict[str, str]] = {}
        passthrough: list[tuple[str, str]] = []
        page, size = 1, 25
        for k, v in query:
            if k.startswith("filter[") and "][" in k:
                idx = k[len("filter[") : k.index("]")]
                triplets.setdefault(idx, {})[k[k.index("][") + 2 : -1]] = v
            elif k in ("page", "page[number]"):
                try:
                    page = max(1, int(v))
                except ValueError:
                    pass
            elif k in ("perPage", "page[size]"):
                try:
                    size = max(1, min(100, int(v)))
                except ValueError:
                    pass
            else:
                passthrough.append((k, v))
        terms = [t["value"] for t in triplets.values() if t.get("key") == "tags" and t.get("value")]
        if not terms:
            return [], list(query), page, size
        rest = list(passthrough)
        n = 0
        for t in triplets.values():
            if t.get("key") == "tags":
                continue
            for part, v in t.items():
                rest.append((f"filter[{n}][{part}]", v))
            n += 1
        return terms, rest, page, size

    async def _list_by_tags(  # noqa: ANN001
        self, terms, rest, page, size, base_url, token, accept_language, client
    ):
        lows = [t.lower() for t in terms]
        matched: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for p in range(1, self._TAG_SCAN_MAX_PAGES + 1):
            q = rest + [
                ("page[number]", str(p)),
                ("page[size]", str(self._TAG_SCAN_PAGE_SIZE)),
            ]
            status, payload = await self._get(
                base_url,
                token,
                handle=None,
                query=q,
                accept_language=accept_language,
                client=client,
            )
            if status >= 400:
                return self._json(
                    status, payload if isinstance(payload, dict) else {"title": "upstream error"}
                )
            rows = (payload.get("data") if isinstance(payload, dict) else None) or []
            scanned += len(rows)
            for r in rows:
                if not isinstance(r, dict):
                    continue
                rec = self.map_read(r)
                titles = [str(t).lower() for t in (rec.get("tags") or [])]
                if all(any(term in title for title in titles) for term in lows):
                    matched.append(rec)
            if len(rows) < self._TAG_SCAN_PAGE_SIZE:
                break
        else:
            truncated = True
        start = (page - 1) * size
        extra: dict[str, Any] = {
            "emulatedFilter": {
                "field": "tags",
                "scannedRows": scanned,
                "matched": len(matched),
                "truncated": truncated,
            }
        }
        # Same envelope the generic list path emits (see base._list_envelope):
        # consumers read the count from meta.total or extra.total depending on
        # their generation, and page through meta.lastPage. Returning data
        # without it left tag-filtered lists unpaginable — a caller asking for
        # 2 of 4 matches got 2 rows and no way to learn there were more.
        meta: dict[str, Any] = {"page": page, "perPage": size}
        if not truncated:
            # Only a completed scan knows the real total. A truncated one has
            # merely counted what it reached, and publishing that as `total`
            # would understate it exactly the way a clamped perPage understates
            # lastPage — extra.emulatedFilter.truncated is the honest signal.
            extra["total"] = len(matched)
            meta["total"] = len(matched)
            meta["lastPage"] = max(1, -(-len(matched) // size))
        return self._json(
            200, {"data": matched[start : start + size], "meta": meta, "extra": extra}
        )

    @staticmethod
    def _split_mpn_filter(
        query: list[tuple[str, str]],
    ) -> tuple[list[str] | None, int, int]:
        """Pull a ``identifiers.manufacturerNumber`` filter out of the query.

        Returns ``(values, page, size)`` — ``values`` is the list of requested
        manufacturer numbers when the query filters ONLY by manufacturerNumber, else
        ``None`` (so a combined query falls through to v3, which answers honestly).
        v3 has no manufacturerNumber filter; ``_list_by_manufacturer_number`` resolves
        it via v2 products."""
        triplets: dict[str, dict[str, str]] = {}
        page, size = 1, 25
        for k, v in query:
            if k.startswith("filter[") and "][" in k:
                idx = k[len("filter[") : k.index("]")]
                triplets.setdefault(idx, {})[k[k.index("][") + 2 : -1]] = v
            elif k in ("page", "page[number]"):
                try:
                    page = max(1, int(v))
                except ValueError:
                    pass
            elif k in ("perPage", "page[size]"):
                try:
                    size = max(1, min(100, int(v)))
                except ValueError:
                    pass
        values: list[str] = []
        other = False
        for t in triplets.values():
            if t.get("key") == _MPN_MODEL_KEY:
                val = t.get("value")
                if val not in (None, ""):
                    values.append(val)
            else:
                other = True
        # Only emulate a standalone manufacturerNumber filter. Combined with other
        # filters, fall through so v3's honest "filter not allowed" surfaces rather
        # than silently dropping the other constraints.
        if not values or other:
            return None, page, size
        return values, page, size

    async def _list_by_manufacturer_number(  # noqa: ANN001
        self, values, page, size, base_url, token, accept_language, client
    ):
        """Resolve a manufacturerNumber filter via v2 products (which supports it),
        then read the matched ids back through the normal v3 path so the rows are in
        the model shape. This is the key that matches a supplier price list (MPN) to
        products; v3 products cannot filter by it."""
        op = "equals" if len(values) == 1 else "in"
        base_params: list[tuple[str, str]] = [
            ("filter[0][key]", _MPN_V2_KEY),
            ("filter[0][op]", op),
        ]
        if op == "equals":
            base_params.append(("filter[0][value]", values[0]))
        else:
            base_params.extend(("filter[0][value][]", v) for v in values)

        ids: list[str] = []
        truncated = False
        url = f"{base_url.rstrip('/')}{_V2_PRODUCTS}"
        for p in range(1, self._V2_SCAN_MAX_PAGES + 1):
            params = base_params + [
                ("page[number]", str(p)),
                ("page[size]", str(self._V2_SCAN_PAGE_SIZE)),
            ]
            try:
                if client is None:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                        resp = await c.get(
                            url, params=params, headers=self._headers(token, accept_language)
                        )
                else:
                    resp = await client.get(
                        url, params=params, headers=self._headers(token, accept_language)
                    )
            except httpx.HTTPError as exc:
                return self._json(502, {"title": f"manufacturerNumber lookup failed: {exc}"})
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = {"title": "manufacturerNumber lookup failed"}
                return self._json(resp.status_code, body if isinstance(body, dict) else {})
            try:
                rows = (resp.json() or {}).get("data") or []
            except ValueError:
                rows = []
            ids.extend(str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id"))
            if len(rows) < self._V2_SCAN_PAGE_SIZE:
                break
        else:
            truncated = True

        start = (page - 1) * size
        recs: list[dict[str, Any]] = []
        for up in ids[start : start + size]:
            status, payload = await self._get(
                base_url,
                token,
                handle=str(up),
                query=[],
                accept_language=accept_language,
                client=client,
            )
            if status < 400:
                rec = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(rec, dict):
                    recs.append(self.map_read(rec))

        extra: dict[str, Any] = {
            "emulatedFilter": {
                "field": _MPN_MODEL_KEY,
                "matched": len(ids),
                "truncated": truncated,
                "via": "v2 products",
            }
        }
        meta: dict[str, Any] = {"page": page, "perPage": size}
        if not truncated:
            extra["total"] = len(ids)
            meta["total"] = len(ids)
            meta["lastPage"] = max(1, -(-len(ids) // size))
        return self._json(200, {"data": recs, "meta": meta, "extra": extra})

    # Status writes go to v2 products via the `isDisabled` flag (v3 products is
    # read-only for status). Verified against upstream: `isDisabled` true/false is
    # accepted (204), but `isDeleted` (archive/un-archive) is rejected (400) — so
    # only deactivate/activate are wired; archive stays a wish. A run command may
    # carry {"disabledReason": "…"}, which the base merges onto the PATCH body.
    action_map = {
        "deactivate": {
            "method": "PATCH",
            "path": "/api/v2/products/{id}",
            "body": {"isDisabled": True},
        },
        "activate": {
            "method": "PATCH",
            "path": "/api/v2/products/{id}",
            # Clear the block reason on reactivate (disabledReason:null; v2 rejects
            # an empty string with 400, so null is the clear).
            "body": {"isDisabled": False, "disabledReason": None},
        },
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    # deactivate/activate flip v2 isDisabled (see action_map);
                    # deactivate accepts an optional command {disabledReason}.
                    self.step_cmd("deactivate", "Deactivate"),
                    self.step_cmd("activate", "Activate"),
                    # archive → v2 isDeleted:true is rejected (400) upstream.
                    self.step_cmd(
                        "archive", "Archive", wish="v2 products rejects isDeleted writes (400)."
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "adjustStock",
                "Adjust stock",
                wish="No public product-level stock adjustment; v1 storageLocations/setTotalStock works per location but is not composed.",
            ),
            self.action_def("duplicate", "Duplicate", wish="No duplicate endpoint upstream."),
            self.action_def(
                "recalculatePurchasePrice",
                "Recalculate purchase price",
                wish="Price recalculation has no public trigger.",
            ),
            self.action_def(
                "syncToChannel",
                "Sync to channel",
                wish="v1 products/{id}/salesChannels assigns a channel; a true sync push is not exposed.",
            ),
            self.action_def(
                "mergeInto", "Merge into", wish="Duplicate merge is a UI-only feature — no API."
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            # Number/SKU can be set on create; changing it afterwards is a
            # sensitive identity change we don't expose (create-only).
            "number": prop(
                "string",
                "Number",
                **_C,
                section="general",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
            ),
            # Writable (v2 isDisabled/isDeleted): active/inactive/archived — so a
            # bulk create/update can set the lock directly, in parallel to the
            # deactivate/activate/archive steps. statusReason → v2 disabledReason.
            "status": prop(
                "select",
                "Status",
                **_CU,
                section="general",
                options=_STATUS_OPTIONS,
                previewable=True,
                # Filter active/inactive (→ v3 isDisabled); the default list is
                # active-only, so this surfaces disabled products on demand.
                filterable=True,
            ),
            "statusReason": prop("string", "Status reason", **_CU, section="general"),
            # v2 create only exposes isShippingCostsProduct/isFee flags, not a
            # free "kind" — service/digital cannot be expressed → read-only wish.
            "kind": prop("select", "Kind", **RO, section="general", options=_KIND_OPTIONS),
            "name": prop(
                "string",
                "Name",
                **_CU,
                section="general",
                filterable=True,
                sortable=True,
                searchable=True,
                previewable=True,
            ),
            "description": prop("string", "Description", **_CU, section="general"),
            "unit": prop("string", "Unit", **_CU, section="general"),
            "category": prop(
                "reference",
                "Category",
                **_CU,
                # The mapped upstream value is the merchandise group
                # (Warengruppe), not the productsCategories tree.
                reference="MerchandiseGroup",
                renderProperty="name",
                section="general",
            ),
            "project": prop(
                "reference",
                "Project",
                **_CU,
                reference="Project",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            # v3 products rejects a tags filter (verified on mvp).
            "tags": tags_prop(writable=False, filterable=False),
            "identifiers": prop(
                "embedded",
                "Identifiers",
                section="identifiers",
                properties={
                    "ean": prop("string", "EAN", **_CU, filterable=True, searchable=True),
                    # v2 write slot is manufacturer.number; v3 read is
                    # manufacturerProductNumber (see map_write / map_read).
                    # Filterable is EMULATED: v3 products has no manufacturerNumber
                    # filter, so `request` routes it to v2 products (which does) to
                    # resolve ids, then reads them via v3 — see _list_by_manufacturer_number.
                    "manufacturerNumber": prop(
                        "string", "Manufacturer number", **_CU, filterable=True
                    ),
                    "hsCode": prop("string", "HS code", **_CU),
                    "countryOfOrigin": prop("string", "Country of origin", **_CU),
                    "external": prop(
                        "collection",
                        "External references",
                        **RO,
                        node={
                            "properties": {
                                "channel": prop(
                                    "reference",
                                    "Channel",
                                    reference="Channel",
                                    renderProperty="name",
                                    **RO,
                                ),
                                "id": prop("string", "External ID", **RO),
                            }
                        },
                    ),
                },
            ),
            "manufacturer": prop(
                "embedded",
                "Manufacturer",
                section="general",
                properties={
                    "name": prop("string", "Name", **_CU),
                    "website": prop("string", "Website", **_CU),
                },
            ),
            "prices": prop(
                "embedded",
                "Prices",
                section="prices",
                properties={
                    # Sale price is composed as a separate salesPrices write (_write).
                    "sale": prop(
                        "embedded",
                        "Sale price",
                        properties={
                            "amount": prop("string", "Amount", **_CU),
                            "currency": prop("string", "Currency", **_CU),
                        },
                    ),
                    "purchase": prop(
                        "embedded",
                        "Purchase price",
                        properties={
                            "amount": prop("string", "Amount", **_CU),
                            "currency": prop("string", "Currency", **_CU),
                            "source": prop("string", "Source", **RO),
                        },
                    ),
                },
            ),
            "tax": prop(
                "embedded",
                "Tax",
                section="tax",
                properties={
                    "rate": prop(
                        "select",
                        "Rate",
                        **_CU,
                        options=[
                            {"value": v, "label": v.capitalize()}
                            for v in ("standard", "reduced", "exempt")
                        ],
                    ),
                    "profile": prop("string", "Tax profile", **RO),
                },
            ),
            "logistics": prop(
                "embedded",
                "Logistics",
                section="logistics",
                properties={
                    "weight": prop(
                        "embedded",
                        "Weight",
                        properties={
                            "value": prop("decimal", "Value", **_CU),
                            "unit": prop("string", "Unit", **_CU),
                        },
                    ),
                    "netWeight": prop(
                        "embedded",
                        "Net weight",
                        properties={
                            "value": prop("decimal", "Value", **_CU),
                            "unit": prop("string", "Unit", **_CU),
                        },
                    ),
                    "dimensions": prop(
                        "embedded",
                        "Dimensions",
                        properties={
                            "length": prop("decimal", "Length", **_CU),
                            "width": prop("decimal", "Width", **_CU),
                            "height": prop("decimal", "Height", **_CU),
                            "unit": prop("string", "Unit", **_CU),
                        },
                    ),
                    "minimumOrderQuantity": prop("integer", "Minimum order quantity", **_CU),
                    "minimumStockQuantity": prop("integer", "Minimum stock quantity", **_CU),
                    "packagingUnits": prop(
                        "collection",
                        "Packaging units",
                        **RO,
                        node={
                            "properties": {
                                "unit": prop("string", "Unit", **RO),
                                "quantity": prop("integer", "Quantity", **RO),
                            }
                        },
                    ),
                },
            ),
            "tracking": prop(
                "embedded",
                "Tracking",
                section="tracking",
                properties={
                    "stock": prop("boolean", "Stock item", **_CU),
                    "batches": prop("boolean", "Batches", **_CU),
                    "serialNumbers": prop("string", "Serial numbers mode", **_CU),
                    "bestBefore": prop("boolean", "Best before", **_CU),
                },
            ),
            "stock": prop(
                "embedded",
                "Stock",
                **RO,
                section="stock",
                properties={
                    "available": prop(
                        "decimal",
                        "Available",
                        **RO,
                        description=(
                            "What may still be sold (upstream 'sellable'). NOT what lies on "
                            "the shelf — see physical — and NOT simply physical minus "
                            "reserved: demand from open sales orders is deducted too, and "
                            "the result is floored at 0. Observed on mvp: physical 6, "
                            "reserved 1, openSalesOrders 3 → available 3."
                        ),
                    ),
                    "physical": prop(
                        "decimal",
                        "Physical",
                        **RO,
                        description="What is actually on the shelf, across all warehouses.",
                    ),
                    "reserved": prop(
                        "decimal",
                        "Reserved",
                        **RO,
                        description=(
                            "The quantity Xentral reports as committed. Do NOT compute "
                            "availability from it: available deducts open order demand, not "
                            "this figure — observed physical 6 / reserved 1 / available 3. "
                            "What exactly Xentral counts here is not documented upstream."
                        ),
                    ),
                    "openSalesOrders": prop(
                        "decimal",
                        "Open sales orders",
                        **RO,
                        description="Quantity demanded by open sales orders, not yet shipped.",
                    ),
                    "producible": prop(
                        "decimal",
                        "Producible",
                        **RO,
                        description=(
                            "How many could be built from the components on hand: the "
                            "minimum over the bill of materials, computed on each "
                            "component's AVAILABLE quantity — not its physical stock. "
                            "Null for a product without a bill of materials. Verified on "
                            "mvp: components at available 17 and 3 → producible 3, while "
                            "the constrained component's physical stock was 6."
                        ),
                    ),
                    "correction": prop(
                        "decimal",
                        "Correction",
                        **RO,
                        description=(
                            "Manual up/down adjustment applied to the stock figure before it "
                            "is published (e.g. a safety buffer held back from a shop). A "
                            "publication mechanic, not a physical fact."
                        ),
                    ),
                    "calculated": prop(
                        "decimal",
                        "Calculated",
                        **RO,
                        description=(
                            "Xentral's own derived stock figure. The spec's example suggests "
                            "physical + correction, but every value measured on mvp tracked "
                            "`available` instead (0/3/17). Undocumented upstream — prefer "
                            "physical or available, whichever the question actually needs."
                        ),
                    ),
                    "pseudo": prop(
                        "decimal",
                        "Pseudo",
                        **RO,
                        description=(
                            "A formula-driven stand-in figure reported instead of the real "
                            "one; null when none is configured."
                        ),
                    ),
                    "incoming": prop(
                        "decimal",
                        "Incoming",
                        **RO,
                        description="Always null — Xentral computes no inbound figure.",
                    ),
                    "belowMinimum": prop(
                        "boolean",
                        "Below minimum",
                        **RO,
                        description=(
                            "available < logistics.minimumStockQuantity. Null when either "
                            "side is unknown — never defaulted to false."
                        ),
                    ),
                },
            ),
            "production": prop(
                "embedded",
                "Production",
                section="production",
                properties={
                    "mode": prop("select", "Mode", **_CU, options=_PRODUCTION_OPTIONS),
                    "hasBillOfMaterials": prop("boolean", "Has bill of materials", **_CU),
                },
            ),
            "documentDefaults": prop(
                "embedded",
                "Document defaults",
                section="general",
                properties={
                    "hidePrice": prop("boolean", "Hide price", **_CU),
                    # Not in the v2 create body → read-only wishes.
                    "noticeText": prop("string", "Notice text", **RO),
                    "requiresCustomerApproval": prop("boolean", "Requires customer approval", **RO),
                },
            ),
            "variant": prop(
                "embedded",
                "Variant",
                section="variant",
                properties={
                    "of": prop(
                        "reference", "Of", **_CU, reference="Product", renderProperty="name"
                    ),
                    "isMatrix": prop(
                        "boolean",
                        "Is matrix",
                        **_CU,
                        filterable=True,
                        description=(
                            "Whether this is the matrix (parent) product of a variant "
                            "set. Filterable — the upstream key is isMatrixProduct."
                        ),
                    ),
                },
            ),
            "bom": prop(
                "embedded",
                "Bill of materials",
                section="production",
                description=(
                    "The product's DIRECT components — one level only. Each component is "
                    "itself a product, so a full explosion means reading each child in "
                    "turn until its bom.items is empty; there is no roll-up upstream. "
                    "Filled on a single read only: in a list bom.items is always empty."
                ),
                properties={
                    "items": prop(
                        "collection",
                        "Items",
                        **_CU,
                        description=(
                            "REPLACES the whole bill of materials on a create/update — this "
                            "is not an append. Send every line you want to keep: a line you "
                            "omit is DELETED, and [] clears the bill entirely. To change one "
                            "quantity, read the product, edit that number, and send all lines "
                            "back. If only this composition fails the product is still "
                            "written and the response carries `_warnings.bom` — a 200/201 "
                            "does not by itself mean the bill was set."
                        ),
                        node={
                            "properties": {
                                "product": prop(
                                    "reference",
                                    "Product",
                                    reference="Product",
                                    renderProperty="name",
                                    **_CU,
                                ),
                                "quantity": prop(
                                    "decimal",
                                    "Quantity",
                                    **_CU,
                                    description=(
                                        "How much of this component ONE unit of the parent "
                                        "needs — not a stock figure."
                                    ),
                                ),
                                "type": prop(
                                    "select",
                                    "Type",
                                    **_CU,
                                    options=[{"value": v, "label": v} for v in _PART_TYPES],
                                    description=(
                                        "Omitted on a write, the upstream assigns "
                                        "'shopping part' (observed on mvp)."
                                    ),
                                ),
                                "reference": prop("string", "Reference", **_CU),
                            }
                        },
                    )
                },
            ),
            "suppliers": prop(
                "collection",
                "Suppliers",
                section="general",
                node={
                    "properties": {
                        # Only the default supplier round-trips to v2's
                        # standardSupplier; per-supplier price/number is a wish.
                        "supplier": prop(
                            "reference",
                            "Supplier",
                            **_CU,
                            reference="Supplier",
                            renderProperty="name",
                        ),
                        "supplierProductNumber": prop("string", "Supplier product number", **RO),
                        "isDefault": prop("boolean", "Default", **_CU),
                    }
                },
            ),
            # Per-product free-field VALUES (Freifelder). Read from v3 include=
            # customFields; write to v2 freeFields[{id=number, value}] (see map_write).
            "customFields": prop(
                "collection",
                "Custom fields",
                section="general",
                node={
                    "properties": {
                        "number": prop("integer", "Field number", **_CU),
                        "label": prop("string", "Label", **RO),
                        "value": prop("string", "Value", **_CU),
                    }
                },
            ),
            # Assigned property values (Eigenschaften). Read hydrates from v1
            # /properties; write composes via v1 PATCH /properties (see _write).
            "properties": prop(
                "collection",
                "Properties",
                section="general",
                node={
                    "properties": {
                        "property": prop(
                            "reference",
                            "Property",
                            **_CU,
                            reference="ProductProperty",
                            renderProperty="name",
                        ),
                        "name": prop("string", "Name", **RO),
                        "value": prop("string", "Value", **_CU),
                        "unit": prop("string", "Unit", **_CU),
                    }
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop(
                "datetime",
                "Updated at",
                **RO,
                sortable=True,
                filterable=True,
                description=(
                    "When the product last changed. Filterable — this is the key for an "
                    "incremental sync: ask for everything changed since the last run "
                    "instead of paging the whole catalogue."
                ),
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        m = r.get("measurements") or {}

        def dim(key: str) -> dict[str, Any] | None:
            # Two upstream generations: {value, unit} objects (pre-2026-07-25)
            # and bare numbers (the reshipped v3 products; unit implicit).
            v = m.get(key)
            if isinstance(v, dict):
                return (
                    {"value": v.get("value"), "unit": v.get("unit")}
                    if v.get("value") is not None
                    else None
                )
            if isinstance(v, (int, float)) and v:
                unit = "kg" if "eight" in key else "cm"  # weight/netWeight vs. dimensions
                return {"value": v, "unit": unit}
            return None

        # Dimensions carry the same two upstream generations as weight —
        # resolve them through dim() so a bare number cannot crash the record.
        length, width, height = dim("length"), dim("width"), dim("height")

        cpp = r.get("calculatedPurchasePrice")
        if not isinstance(cpp, dict):
            cpp = {}
        pp_price = cpp.get("price")
        if not isinstance(pp_price, dict):
            pp_price = {}
        supplier = r.get("defaultSupplier") or r.get("standardSupplier")
        # v3 carries the manufacturer NAME as a bare string (``manufacturer``); the
        # website is a separate top-level field (``manufacturerUrl``) and the
        # manufacturer's product number is ``manufacturerProductNumber``. (v1/v2
        # nest name/link under a ``manufacturer`` object — tolerate that too.)
        man = r.get("manufacturer")
        man_name = man.get("name") if isinstance(man, dict) else man
        man_url = r.get("manufacturerUrl") or (man.get("link") if isinstance(man, dict) else None)
        return {
            "object": "product",
            "id": (f"prd_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("number"),
            "status": _status(r),
            "statusReason": r.get("disabledReason"),
            "kind": _kind(r),
            "name": r.get("name"),
            "description": r.get("description"),
            "unit": r.get("unit"),
            # The upstream value IS the merchandise group (Warengruppe) — emit
            # the MerchandiseGroup entity's prefix so the reference resolves.
            "category": ref(
                "mg_",
                (r.get("merchandiseGroup") or {}).get("id"),
                None,
                (r.get("merchandiseGroup") or {}).get("name"),
                "productsMerchandiseGroups",
            ),
            "project": ref(
                "prj_",
                (r.get("project") or {}).get("id"),
                None,
                (r.get("project") or {}).get("name"),
                "projects",
            ),
            "tags": map_tags(r.get("tags")),
            "identifiers": {
                "ean": r.get("ean"),
                "manufacturerNumber": r.get("manufacturerProductNumber"),
                "hsCode": r.get("customsTariffNumber"),
                "countryOfOrigin": r.get("countryOfOrigin"),
                "external": [],
            },
            "manufacturer": {"name": man_name or None, "website": man_url},
            "prices": {
                "sale": None,
                "purchase": money(pp_price.get("amount"), pp_price.get("currency") or "EUR"),
            },
            "tax": {
                "rate": _tax_rate(r),
                "profile": None,
            },
            "logistics": {
                "weight": dim("weight"),
                "netWeight": dim("netWeight"),
                "dimensions": (
                    {
                        "length": length["value"],
                        "width": (width or {}).get("value"),
                        "height": (height or {}).get("value"),
                        "unit": length["unit"],
                    }
                    if length
                    else None
                ),
                "minimumOrderQuantity": r.get("minimumOrderQuantity"),
                # v3 read exposes it as ``minimumStockLevel``; v2 WRITE takes
                # ``minimumStorageQuantity`` (see map_write) — different names.
                "minimumStockQuantity": r.get("minimumStockLevel"),
                "packagingUnits": [],
            },
            "tracking": {
                "stock": r.get("isStockItem"),
                "batches": r.get("hasBatches"),
                # v3 read: ``serialNumberTracking``; v2 write: ``serialNumbersMode``
                # (different enums — see map_write; a value round-trips only for the
                # names v3 also uses).
                "serialNumbers": r.get("serialNumberTracking"),
                "bestBefore": r.get("hasBestBeforeDate"),
            },
            "stock": dict.fromkeys(_STOCK_KEYS),
            "production": {
                "mode": _production_mode(r),
                "hasBillOfMaterials": r.get("hasBillOfMaterials"),
            },
            "documentDefaults": {
                # v3 read nests it under ``printSettings.withoutPrices``; v2 WRITE
                # takes the flat ``hidePriceOnDocuments`` (see map_write).
                "hidePrice": (r.get("printSettings") or {}).get("withoutPrices"),
                "noticeText": r.get("noticeText"),
                "requiresCustomerApproval": r.get("requiresCustomerApproval"),
            },
            "variant": {
                "of": ref("prd_", (r.get("variantOf") or {}).get("id"), None, None, "products"),
                "isMatrix": r.get("isMatrixProduct"),
            },
            "bom": {"items": []},
            "suppliers": (
                [
                    {
                        "supplier": ref("sup_", supplier.get("id"), None, None, "suppliers"),
                        "supplierProductNumber": None,
                        "isDefault": True,
                    }
                ]
                if isinstance(supplier, dict) and supplier.get("id")
                else []
            ),
            "customFields": map_custom_fields(r.get("customFields")),
            # properties hydrate on `get` (v1 /properties); empty on list reads.
            "properties": [],
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # Top-level model keys we actively map onto the v2 create/update body.
    _WRITABLE = {
        "name",
        "number",
        "description",
        "unit",
        # status/statusReason → v2 isDisabled/isDeleted/disabledReason (map_write).
        "status",
        "statusReason",
        "category",
        "project",
        "identifiers",
        "manufacturer",
        "prices",
        "tax",
        "logistics",
        "tracking",
        "production",
        "documentDefaults",
        "variant",
        "suppliers",
        # customFields ARE mapped (→ v2 freeFields, see map_write).
        "customFields",
        # `bom` and `properties` are accepted but NOT put in the v2 body — they are
        # composed on their own sub-resources in _write (like prices.sale). Listed
        # here so a write carrying them is not rejected as an unknown key.
        "bom",
        "properties",
    }
    # Keys the read emits (so round-trip writes carry them) but that are
    # system/computed or schema read-only — accepted silently, never sent. The
    # field schema (creatable/updatable) is the contract; unknown keys still 409.
    _IGNORE = {
        "object",
        "id",
        "kind",
        "stock",
        "tags",
        "createdAt",
        "updatedAt",
    }

    @staticmethod
    def _ref_id(value: Any) -> str | None:
        """A model reference ({id: "mg_7"} or a bare id) → the bare numeric
        upstream id (speaking prefix stripped, ADR-002). None clears it."""
        ident = value.get("id") if isinstance(value, dict) else value
        if ident in (None, ""):
            return None
        ident = str(ident)
        return ident.split("_", 1)[1] if "_" in ident else ident

    @staticmethod
    def _measure(obj: Any, default_unit: str) -> dict[str, Any] | None:
        """A model {value, unit} → the v2 measurements shape {value, unit}.
        Tolerates a bare number. None when there is no value to send."""
        if isinstance(obj, dict):
            v, u = obj.get("value"), obj.get("unit")
        elif isinstance(obj, (int, float)):
            v, u = obj, None
        else:
            return None
        if v in (None, ""):
            return None
        return {"value": v, "unit": u or default_unit}

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """Map the new model onto the v2 products create/update body (field names
        grounded in the v2 OpenAPI request schema). Sale price is NOT part of the
        body — it is composed as a separate salesPrices write in ``_write`` — so
        ``prices.sale`` is intentionally not mapped here. Fields the v2 body cannot
        accept stay schema read-only; unknown top-level keys are rejected (409)."""
        v2: dict[str, Any] = {}
        rejected: set[str] = set()

        # --- scalars / references -----------------------------------------
        for key in ("name", "number", "description", "unit"):
            if model.get(key) is not None:
                v2[key] = model[key]
        # --- status lock (v2 isDisabled) + reason --------------------------
        # active ↔ inactive flip v2 `isDisabled` — set directly in a create/update
        # body so a bulk import can lock records too. `archived` maps to isDeleted,
        # which v2 REJECTS (400), so it is intentionally not emitted here (archived
        # stays read-only, and round-trip writes of an archived record are a no-op).
        status = model.get("status")
        if status == "inactive":
            v2["isDisabled"] = True
        elif status == "active" and not creating:
            # Reactivate: unblock and clear the reason (null; "" is rejected 400).
            # Only on UPDATE — a new product is active by default, and the v2
            # CREATE rejects disabledReason:null (create wants a string, not null).
            v2["isDisabled"] = False
            v2["disabledReason"] = None
        # An explicit statusReason still wins (e.g. deactivate-with-reason).
        if model.get("statusReason") is not None:
            v2["disabledReason"] = model["statusReason"]
        if "category" in model:  # model category == merchandise group (Warengruppe)
            mg = self._ref_id(model["category"])
            if mg is not None:
                v2["merchandiseGroup"] = {"id": mg}
        # v2 REQUIRES a project on create; fall back to the standard project.
        proj = self._ref_id(model.get("project")) if "project" in model else None
        if proj is not None:
            v2["project"] = {"id": proj}
        elif creating:
            v2["project"] = {"id": _DEFAULT_PROJECT_ID}

        # --- identifiers ---------------------------------------------------
        idents = model.get("identifiers") or {}
        if isinstance(idents, dict):
            if idents.get("ean") is not None:
                v2["ean"] = idents["ean"]
            if idents.get("hsCode") is not None:
                v2["customsTariffNumber"] = idents["hsCode"]
            if idents.get("countryOfOrigin") is not None:
                v2["countryOfOrigin"] = idents["countryOfOrigin"]

        # --- manufacturer (name + website→link + number) -------------------
        # The v2 body nests the manufacturer product number under manufacturer.number
        # (the model exposes it as identifiers.manufacturerNumber, which v3 reads back
        # as manufacturerProductNumber).
        man = model.get("manufacturer") or {}
        mv: dict[str, Any] = {}
        if isinstance(man, dict):
            if man.get("name") is not None:
                mv["name"] = man["name"]
            if man.get("website") is not None:
                mv["link"] = man["website"]
        mnum = idents.get("manufacturerNumber") if isinstance(idents, dict) else None
        if mnum is not None:
            mv["number"] = mnum
        if mv:
            v2["manufacturer"] = mv

        # --- purchase price (sale price is composed separately) ------------
        prices = model.get("prices") or {}
        purchase = prices.get("purchase") if isinstance(prices, dict) else None
        if isinstance(purchase, dict) and purchase.get("amount") is not None:
            v2["calculatedPurchasePrice"] = {
                # source == "calculated" → auto-calculated; otherwise a manual price.
                "hasCalculatedPurchasePrice": purchase.get("source") == "calculated",
                "price": {
                    "amount": str(purchase["amount"]),
                    "currency": purchase.get("currency") or "EUR",
                },
            }

        # --- tax -----------------------------------------------------------
        tax = model.get("tax") or {}
        if isinstance(tax, dict) and tax.get("rate") is not None:
            mapped = _TAX_TO_V2.get(tax["rate"])
            if mapped is not None:
                v2["salesTax"] = mapped

        # --- logistics -----------------------------------------------------
        log = model.get("logistics") or {}
        if isinstance(log, dict):
            measurements: dict[str, Any] = {}
            w = self._measure(log.get("weight"), "kg")
            if w:
                measurements["weight"] = w
            nw = self._measure(log.get("netWeight"), "kg")
            if nw:
                measurements["netWeight"] = nw
            dims = log.get("dimensions") or {}
            if isinstance(dims, dict):
                unit = dims.get("unit") or "cm"
                for axis in ("length", "width", "height"):
                    if dims.get(axis) is not None:
                        measurements[axis] = {"value": dims[axis], "unit": unit}
            if measurements:
                v2["measurements"] = measurements
            if log.get("minimumOrderQuantity") is not None:
                v2["minimumOrderQuantity"] = log["minimumOrderQuantity"]
            if log.get("minimumStockQuantity") is not None:
                v2["minimumStorageQuantity"] = log["minimumStockQuantity"]

        # --- tracking flags ------------------------------------------------
        tr = model.get("tracking") or {}
        if isinstance(tr, dict):
            if tr.get("stock") is not None:
                v2["isStockItem"] = bool(tr["stock"])
            if tr.get("batches") is not None:
                v2["hasBatches"] = bool(tr["batches"])
            if tr.get("bestBefore") is not None:
                v2["hasBestBeforeDate"] = bool(tr["bestBefore"])
            sn = tr.get("serialNumbers")
            if sn is not None:
                v2["serialNumbersMode"] = sn if sn in _SN_MODES else "disabled"

        # --- production ----------------------------------------------------
        prod = model.get("production") or {}
        if isinstance(prod, dict):
            if prod.get("hasBillOfMaterials") is not None:
                v2["hasBillOfMaterials"] = bool(prod["hasBillOfMaterials"])
            mode = prod.get("mode")
            if mode is not None:
                v2["isAssembledJustInTime"] = mode == "justInTime"
                v2["isExternallyProduced"] = mode == "external"
                v2["isProductionProduct"] = mode == "inHouse"

        # --- kind → shipping-cost flag (the only kind v2 create exposes) ----
        if model.get("kind") == "shippingCost":
            v2["isShippingCostsProduct"] = True

        # --- document defaults (only hidePrice round-trips) ----------------
        dd = model.get("documentDefaults") or {}
        if isinstance(dd, dict) and dd.get("hidePrice") is not None:
            v2["hidePriceOnDocuments"] = bool(dd["hidePrice"])

        # --- variant -------------------------------------------------------
        variant = model.get("variant") or {}
        if isinstance(variant, dict):
            if variant.get("isMatrix") is not None:
                v2["isMatrixProduct"] = bool(variant["isMatrix"])
            vof = self._ref_id(variant.get("of"))
            if vof is not None:
                v2["variantOf"] = {"id": vof}

        # --- default supplier (only the isDefault entry → standardSupplier) -
        suppliers = model.get("suppliers")
        if isinstance(suppliers, list):
            default = next(
                (s for s in suppliers if isinstance(s, dict) and s.get("isDefault")),
                None,
            ) or (suppliers[0] if suppliers and isinstance(suppliers[0], dict) else None)
            sup_id = self._ref_id(default.get("supplier")) if isinstance(default, dict) else None
            if sup_id is not None:
                v2["standardSupplier"] = {"id": sup_id}

        # --- custom fields → v2 freeFields[{id=<slot number>, value}] ------
        # (model customFields carry the free-field slot as `number`; v3 reads them
        # back under key customField<number>.) properties are composed separately.
        cf = model.get("customFields")
        if isinstance(cf, list):
            free_fields = [
                {
                    "id": str(it["number"]),
                    "value": "" if it.get("value") is None else str(it["value"]),
                }
                for it in cf
                if isinstance(it, dict) and it.get("number") is not None
            ]
            if free_fields:
                v2["freeFields"] = free_fields

        # --- reject genuinely unknown top-level keys -----------------------
        for k in model:
            if k not in self._WRITABLE and k not in self._IGNORE:
                rejected.add(k)
        return v2, rejected

    # ---- sale-price composition -----------------------------------------
    # Sale price is a separate resource. Primary is v3 salesPrices (model-aligned);
    # v1 salesPrices is the stable fallback (v3 is Beta and may be absent, or the
    # token may lack the salesPrice:create scope, on a given tenant). The ONLY body
    # difference is the quantity-tier key: v3 calls it ``quantity`` (required), v1
    # calls it ``amount`` — both grounded in the OpenAPI create schemas.
    # ``_post_sale_price`` tries each path in order and returns the first success
    # (or the last failure if every attempt failed).
    _sale_price_paths = (_SALES_PRICE_PATH, _SALES_PRICE_PATH_FALLBACK)
    _sale_tier_key = {
        _SALES_PRICE_PATH: "quantity",
        _SALES_PRICE_PATH_FALLBACK: "amount",
    }
    # Only retry the next path when v3 looks unavailable/not-permitted (not deployed,
    # missing scope, method not allowed). A real validation answer (400/409/422/429)
    # is honest and surfaces as-is rather than being re-posted against the v1 schema.
    _sale_price_fallback_statuses = frozenset({403, 404, 405, 501})

    def _sale_price_payload(self, up_id: str, sale: Any, *, tier_key: str) -> dict[str, Any] | None:
        """salesPrices create body for the product's base (quantity-1) price.
        ``tier_key`` is the version's name for the quantity tier (v3 ``quantity`` /
        v1 ``amount``). None when there is no amount to write. Left open (no
        validFrom): v3 rejects validFrom without a later expiresAt, and an update
        already ends the prior price yesterday, so an open new price is the single
        effective one."""
        if not isinstance(sale, dict) or sale.get("amount") in (None, ""):
            return None
        return {
            "product": {"id": str(up_id)},
            tier_key: 1,  # base tier — quantity 1 = the product's list price
            "price": {
                "amount": str(sale["amount"]),
                "currency": sale.get("currency") or "EUR",
            },
        }

    async def _post_sale_price(
        self,
        up_id: str,
        sale: Any,
        base_url: str,
        token: str,
        accept_language: str | None,
        client,  # noqa: ANN001
    ) -> tuple[int, Any]:
        """POST the base sale price, trying v3 first then the v1 fallback. Returns
        the first success, or the last response when every path failed. Falls back to
        the next path only when v3 is unavailable/not-permitted (see
        ``_sale_price_fallback_statuses``); a real validation error surfaces as-is.
        (0, {}) means there was nothing to write."""
        headers = self._headers(token, accept_language)
        last: tuple[int, Any] = (0, {})
        for path in self._sale_price_paths:
            payload = self._sale_price_payload(up_id, sale, tier_key=self._sale_tier_key[path])
            if payload is None:
                return 0, {}  # nothing to write
            url = f"{base_url.rstrip('/')}{path}"

            async def _do(c, _url=url, _payload=payload):  # noqa: ANN001
                return await c.post(_url, json=_payload, headers=headers)

            if client is None:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    resp = await _do(c)
            else:
                resp = await _do(client)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            last = (resp.status_code, body)
            if resp.status_code < 400 or resp.status_code not in self._sale_price_fallback_statuses:
                return last
        return last

    async def _supersede_standard_sale_price(  # noqa: ANN001
        self, up_id, new_amount, new_currency, base_url, token, accept_language, client
    ) -> bool:
        """Before writing a new standard sale price on UPDATE, end-date the current
        standard price(s) so the product keeps ONE effective price plus history
        (creating a second salesPrice would otherwise leave two "Standardpreis"
        rows). Every OPEN standard tier (no customer/group, quantity 1) whose amount
        differs from the target is validity-ended yesterday via the v3 salesPrices
        collection PATCH — this also closes accumulated duplicates. Returns True if a
        new price should still be posted; False when an open standard price already
        equals the target (no-op, no duplicate)."""

        def _r2(v: Any) -> float | None:
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        headers = self._headers(token, accept_language)
        url = f"{base_url.rstrip('/')}/api/v1/products/{up_id}/salesPrices"
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                    resp = await c.get(url, headers=headers)
            else:
                resp = await client.get(url, headers=headers)
            rows = resp.json().get("data") if resp.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            rows = None
        if not isinstance(rows, list):
            return True  # can't read the current prices → fall back to a plain POST

        target = (_r2(new_amount), (new_currency or "EUR"))
        close_ids: list[Any] = []
        already = False
        for r in rows:
            if not isinstance(r, dict) or r.get("customer") or r.get("customerGroup"):
                continue
            if _r2(r.get("amount")) != 1.0 or not _valid_on(r, date.today()):
                continue  # only the OPEN base (quantity-1) tier
            price = r.get("price") if isinstance(r.get("price"), dict) else {}
            if (_r2(price.get("amount")), (price.get("currency") or "EUR")) == target:
                already = True
            elif r.get("id") is not None:
                close_ids.append(r["id"])

        if close_ids:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            patch = [{"id": cid, "expiresAt": yesterday} for cid in close_ids]
            purl = f"{base_url.rstrip('/')}{_SALES_PRICE_PATH}"
            try:
                if client is None:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                        await c.patch(purl, json=patch, headers=headers)
                else:
                    await client.patch(purl, json=patch, headers=headers)
            except httpx.HTTPError:
                pass
        return not already

    async def _write(  # noqa: ANN001
        self, method, handle, query, body, base_url, token, accept_language, client
    ):
        """Create/update the product via v2 (base ``_write`` → ``write_path``), then
        compose the resources the v2 body cannot carry — the sale price (salesPrices),
        the bill of materials (/parts) and the assigned property values (/properties).
        A composition failure AFTER a successful product write is a partial success
        (the product exists): reported as a warning, never a silent drop, never a
        false error."""
        try:
            model = json.loads(body or b"{}")
        except (ValueError, TypeError):
            model = {}
        if not isinstance(model, dict):
            model = {}
        sale = (model.get("prices") or {}).get("sale")
        bom_items = (model.get("bom") or {}).get("items")
        prop_items = model.get("properties")

        resp = await super()._write(
            method, handle, query, body, base_url, token, accept_language, client
        )
        is_dry = any(k == "dryRun" and v in ("true", "1") for k, v in query)
        has_sale = isinstance(sale, dict) and sale.get("amount") not in (None, "")
        compose_bom = isinstance(bom_items, list)
        compose_props = isinstance(prop_items, list)
        if resp.status_code >= 400 or is_dry or not (has_sale or compose_bom or compose_props):
            return resp

        try:
            data = json.loads(resp.content or b"{}").get("data") or {}
        except (ValueError, TypeError):
            data = {}
        up_id = self._ref_id(data.get("id"))
        if not up_id:
            return resp  # no id to attach the composed resources to

        warnings: dict[str, Any] = {}
        if has_sale:
            # On UPDATE, end-date the current standard price(s) first so the product
            # keeps ONE effective standard price + history (and accumulated duplicates
            # are cleaned up); the new price is posted open, which — with the old ones
            # ended yesterday — is the single effective price. On CREATE there is
            # nothing to supersede. (The new price is left open rather than dated from
            # today: v3 salesPrices rejects validFrom without a later expiresAt.)
            should_post = True
            if method != "POST":
                should_post = await self._supersede_standard_sale_price(
                    str(up_id),
                    sale["amount"],
                    sale.get("currency"),
                    base_url,
                    token,
                    accept_language,
                    client,
                )
            st, pr = (
                await self._post_sale_price(
                    str(up_id), sale, base_url, token, accept_language, client
                )
                if should_post
                else (0, {})
            )
            if st >= 400:
                warnings["salePrice"] = {
                    "message": (
                        "Product was created/updated, but setting the sale price failed. "
                        "Retry the sale price via the salesPrices resource."
                    ),
                    "status": st,
                    "error": pr if isinstance(pr, dict) else {"raw": str(pr)[:300]},
                }
            else:
                # The v3 read does not surface the sale price — stamp what we persisted.
                data.setdefault("prices", {})["sale"] = {
                    "amount": str(sale["amount"]),
                    "currency": sale.get("currency") or "EUR",
                }
        if compose_bom:
            ok, berr = await self._compose_bom(
                str(up_id), bom_items, base_url, token, accept_language, client
            )
            if not ok:
                warnings["bom"] = {
                    "message": (
                        "Product was created/updated, but setting the bill of materials "
                        "failed. Retry via the product's parts resource."
                    ),
                    "error": berr if isinstance(berr, dict) else {"raw": str(berr)[:300]},
                }
            else:
                # /parts is only hydrated on `get` — stamp the parts we just set.
                data["bom"] = {"items": self._stamp_bom(bom_items)}
        if compose_props:
            ok, perr = await self._compose_properties(
                str(up_id), prop_items, base_url, token, accept_language, client
            )
            if not ok:
                warnings["properties"] = {
                    "message": (
                        "Product was created/updated, but setting the property values "
                        "failed. Retry via the product's properties resource."
                    ),
                    "error": perr if isinstance(perr, dict) else {"raw": str(perr)[:300]},
                }
            else:
                # properties hydrate only on `get` — stamp what we just set.
                data["properties"] = self._stamp_properties(prop_items)
        if warnings:
            data["_warnings"] = warnings
        return self._json(resp.status_code, {"data": data})

    # ---- bill-of-materials composition (/products/{id}/parts) ------------
    def _stamp_bom(self, items: Any) -> list[dict[str, Any]]:
        """The set parts, in the model's bom.items shape, to stamp onto the write
        response (the v3 read does not carry parts; they hydrate only on `get`)."""
        out: list[dict[str, Any]] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            pid = self._ref_id(it.get("product"))
            if pid is None:
                continue
            q = it.get("quantity")
            out.append(
                {
                    "product": ref("prd_", pid, None, None, "products"),
                    "quantity": _num(q) if isinstance(q, (int, float)) else q,
                    "type": it.get("type"),
                    "reference": it.get("reference") or None,
                }
            )
        return out

    async def _parts_call(  # noqa: ANN001
        self, method, url, token, accept_language, client, payload=None
    ) -> tuple[int, Any]:
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=60.0) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    async def _compose_bom(  # noqa: ANN001
        self, up_id, items, base_url, token, accept_language, client
    ) -> tuple[bool, Any]:
        """SET the product's parts to ``items``: POST the desired parts, THEN delete
        the previously existing lines. POST-before-DELETE keeps a failed POST
        non-destructive (the old BOM stays intact). An empty ``items`` clears the BOM.
        Returns (ok, error)."""
        root = base_url.rstrip("/")
        desired: list[dict[str, Any]] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            pid = self._ref_id(it.get("product"))
            if pid is None:
                continue
            q = it.get("quantity")
            part: dict[str, Any] = {"part": {"id": pid}, "amount": q if q not in (None, "") else 1}
            if it.get("type") in _PART_TYPES:
                part["type"] = it["type"]
            if it.get("reference") is not None:
                part["reference"] = it["reference"]
            desired.append(part)

        # current part-line ids (best effort — a read failure just means nothing to prune)
        cst, cur = await self._parts_call(
            "GET", root + _PARTS_V1.format(id=up_id), token, accept_language, client
        )
        old_ids = [
            str(r["id"])
            for r in ((cur.get("data") if isinstance(cur, dict) else None) or [])
            if isinstance(r, dict) and r.get("id") is not None
        ]

        if desired:
            pst, ppl = await self._parts_call(
                "POST", root + _PARTS_V2.format(id=up_id), token, accept_language, client, desired
            )
            if pst >= 400:
                return False, ppl  # old BOM untouched
        if old_ids:
            dst, dpl = await self._parts_call(
                "DELETE",
                root + _PARTS_V1.format(id=up_id),
                token,
                accept_language,
                client,
                [{"id": i} for i in old_ids],
            )
            if dst >= 400:
                return False, dpl
        return True, None

    # ---- property-value composition (/products/{id}/properties) ----------
    def _stamp_properties(self, items: Any) -> list[dict[str, Any]]:
        """The set property values, in the model's properties shape, stamped onto the
        write response (properties hydrate only on `get`)."""
        out: list[dict[str, Any]] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            pid = self._ref_id(it.get("property"))
            if pid is None:
                continue
            out.append(
                {
                    "property": ref("pprop_", pid, None, None, "productsProperties"),
                    "name": it.get("name"),
                    "value": it.get("value"),
                    "unit": it.get("unit"),
                }
            )
        return out

    async def _compose_properties(  # noqa: ANN001
        self, up_id, items, base_url, token, accept_language, client
    ) -> tuple[bool, Any]:
        """Upsert the product's property values via v1 PATCH /properties. An empty
        list is a no-op (there is no per-value delete endpoint). Returns (ok, error)."""
        body: list[dict[str, Any]] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            pid = self._ref_id(it.get("property"))
            if pid is None:
                continue
            entry: dict[str, Any] = {
                "property": {"id": pid},
                "value": "" if it.get("value") is None else str(it["value"]),
            }
            if it.get("unit") is not None:
                entry["unit"] = it["unit"]
            body.append(entry)
        if not body:
            return True, None  # nothing to set
        st, pl = await self._parts_call(
            "PATCH",
            base_url.rstrip("/") + _PROPERTIES_V1.format(id=up_id),
            token,
            accept_language,
            client,
            body,
        )
        return (st < 400), (None if st < 400 else pl)
