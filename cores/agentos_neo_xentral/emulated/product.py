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
(field names grounded in the v2 OpenAPI request schema). Fields the v2 body does
not accept (manufacturerNumber, packaging units, custom fields, BOM parts, stock)
stay read-only and surface as blue wishes (ADR-014).

**Sale price** is not part of the product body upstream — it is a separate
resource (``POST /api/v3/salesPrices``, v1 fallback). ``_write`` composes it on
top of a successful create/update: the product is created first, then the sale
price is posted against the new product id. A price failure after a successful
product create is reported honestly as a partial success (never a false 201).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, map_tags, money, prop, ref, tags_prop

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
        # Read via v3 (PR #24325, read-only by design); WRITE via v2 products
        # (POST/PATCH /api/v2/products — see write_path). Sale price is a separate
        # resource composed on top of the create (see _write).
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/products"
    # v3 products is read-only; create/update go to v2 products (product.createV2 /
    # product.updateV2). The base's _send targets this for POST/PATCH; reads stay v3.
    write_path = "/api/v2/products"
    include = "project,defaultSupplier,merchandiseGroup,tags"
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
        return await super().request(
            method=method,
            handle=handle,
            query=query,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )

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
        extra = {
            "emulatedFilter": {
                "field": "tags",
                "scannedRows": scanned,
                "matched": len(matched),
                "truncated": truncated,
            }
        }
        return self._json(200, {"data": matched[start : start + size], "extra": extra})

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "deactivate",
                        "Deactivate",
                        wish="v3 products is read-only — status writes need a write endpoint.",
                    ),
                    self.step_cmd(
                        "activate",
                        "Activate",
                        wish="v3 products is read-only — status writes need a write endpoint.",
                    ),
                    self.step_cmd(
                        "archive",
                        "Archive",
                        wish="Archiving is not exposed; v3 products is read-only.",
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
            # Status transitions run through the deactivate/activate steps, not a
            # free field write; archived is not settable via v2 → read-only here.
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=_STATUS_OPTIONS,
                previewable=True,
            ),
            "statusReason": prop("string", "Status reason", **RO, section="general"),
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
            ),
            "tags": tags_prop(writable=False),
            "identifiers": prop(
                "embedded",
                "Identifiers",
                section="identifiers",
                properties={
                    "ean": prop("string", "EAN", **_CU, filterable=True, searchable=True),
                    # manufacturerNumber has no slot in the v2 create body → wish.
                    "manufacturerNumber": prop("string", "Manufacturer number", **RO),
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
                    "available": prop("integer", "Available", **RO),
                    "reserved": prop("integer", "Reserved", **RO),
                    "incoming": prop("integer", "Incoming", **RO),
                    "belowMinimum": prop("boolean", "Below minimum", **RO),
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
                    "isMatrix": prop("boolean", "Is matrix", **_CU),
                },
            ),
            "bom": prop(
                "embedded",
                "Bill of materials",
                **RO,
                section="production",
                properties={
                    "items": prop(
                        "collection",
                        "Items",
                        **RO,
                        node={
                            "properties": {
                                "product": prop(
                                    "reference",
                                    "Product",
                                    reference="Product",
                                    renderProperty="name",
                                    **RO,
                                ),
                                "quantity": prop("decimal", "Quantity", **RO),
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
            "customFields": prop("embedded", "Custom fields", section="general", properties={}),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
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
        # v1 frequently carries the manufacturer as a bare name string (it is
        # a free-text field there); the {name, link} object shape is not
        # guaranteed per record.
        manufacturer = r.get("manufacturer")
        if isinstance(manufacturer, str):
            manufacturer = {"name": manufacturer or None}
        elif not isinstance(manufacturer, dict):
            manufacturer = {}
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
                "manufacturerNumber": r.get("manufacturerNumber"),
                "hsCode": r.get("customsTariffNumber"),
                "countryOfOrigin": r.get("countryOfOrigin"),
                "external": [],
            },
            "manufacturer": {"name": manufacturer.get("name"), "website": manufacturer.get("link")},
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
                "minimumStockQuantity": r.get("minimumStorageQuantity"),
                "packagingUnits": [],
            },
            "tracking": {
                "stock": r.get("isStockItem"),
                "batches": r.get("hasBatches"),
                "serialNumbers": r.get("serialNumbersMode"),
                "bestBefore": r.get("hasBestBeforeDate"),
            },
            "stock": {"available": None, "reserved": None, "incoming": None, "belowMinimum": None},
            "production": {
                "mode": _production_mode(r),
                "hasBillOfMaterials": r.get("hasBillOfMaterials"),
            },
            "documentDefaults": {
                "hidePrice": r.get("hidePriceOnDocuments"),
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
            "customFields": {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # Top-level model keys we actively map onto the v2 create/update body.
    _WRITABLE = {
        "name",
        "number",
        "description",
        "unit",
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
    }
    # Keys the read emits (so round-trip writes carry them) but that are
    # system/computed or schema read-only — accepted silently, never sent. The
    # field schema (creatable/updatable) is the contract; unknown keys still 409.
    _IGNORE = {
        "object",
        "id",
        "status",
        "statusReason",
        "kind",
        "stock",
        "bom",
        "customFields",
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

        # --- manufacturer (name + website→link) ----------------------------
        man = model.get("manufacturer") or {}
        if isinstance(man, dict):
            mv: dict[str, Any] = {}
            if man.get("name") is not None:
                mv["name"] = man["name"]
            if man.get("website") is not None:
                mv["link"] = man["website"]
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

        # --- reject genuinely unknown top-level keys -----------------------
        for k in model:
            if k not in self._WRITABLE and k not in self._IGNORE:
                rejected.add(k)
        return v2, rejected

    # ---- sale-price composition -----------------------------------------
    # Sale price is a separate resource. v1 salesPrices is field-verified here
    # (product + amount [quantity tier] + price); v3 salesPrices exists too and is
    # the model-aligned target once its body is verified live.
    _sale_price_path = _SALES_PRICE_PATH_FALLBACK

    def _sale_price_payload(self, up_id: str, sale: Any) -> dict[str, Any] | None:
        """v1 salesPrices create body for the product's base (quantity-1) price.
        None when there is no amount to write."""
        if not isinstance(sale, dict) or sale.get("amount") in (None, ""):
            return None
        return {
            "product": {"id": str(up_id)},
            "amount": 1,  # quantity tier — 1 = the base sale price
            "price": {
                "amount": str(sale["amount"]),
                "currency": sale.get("currency") or "EUR",
            },
        }

    async def _post_sale_price(
        self,
        payload: dict[str, Any],
        base_url: str,
        token: str,
        accept_language: str | None,
        client,  # noqa: ANN001
    ) -> tuple[int, Any]:
        url = f"{base_url.rstrip('/')}{self._sale_price_path}"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001
            return await c.post(url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=60.0) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    async def _write(  # noqa: ANN001
        self, method, handle, query, body, base_url, token, accept_language, client
    ):
        """Create/update the product via v2 (base ``_write`` → ``write_path``),
        then compose the sale price as a separate salesPrices write. A price
        failure AFTER a successful product write is reported as a partial success
        (the product exists) — never a silent drop and never a false error."""
        try:
            model = json.loads(body or b"{}")
        except (ValueError, TypeError):
            model = {}
        sale = (model.get("prices") or {}).get("sale") if isinstance(model, dict) else None

        resp = await super()._write(
            method, handle, query, body, base_url, token, accept_language, client
        )
        is_dry = any(k == "dryRun" and v in ("true", "1") for k, v in query)
        payload = self._sale_price_payload("0", sale) if sale else None
        if resp.status_code >= 400 or is_dry or payload is None:
            return resp

        try:
            data = json.loads(resp.content or b"{}").get("data") or {}
        except (ValueError, TypeError):
            data = {}
        up_id = self._ref_id(data.get("id"))
        if not up_id:
            return resp  # no id to attach a price to — leave the product response as is
        payload["product"]["id"] = str(up_id)

        st, pr = await self._post_sale_price(payload, base_url, token, accept_language, client)
        if st >= 400:
            data["_warnings"] = {
                "salePrice": {
                    "message": (
                        "Product was created/updated, but setting the sale price failed. "
                        "Retry the sale price via the salesPrices resource."
                    ),
                    "status": st,
                    "error": pr if isinstance(pr, dict) else {"raw": str(pr)[:300]},
                }
            }
            return self._json(resp.status_code, {"data": data})

        # The v3 read does not surface the sale price (map_read leaves it null), so
        # stamp the value we just persisted onto the returned record.
        data.setdefault("prices", {})["sale"] = {
            "amount": str(sale["amount"]),
            "currency": sale.get("currency") or "EUR",
        }
        return self._json(resp.status_code, {"data": data})
