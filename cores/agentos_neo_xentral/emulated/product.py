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
update) is expected to go through v2 products (docs/03-mapping-layer.md), but that
payload is not yet field-verified here — every write is a blue wish until that
mapping is built and proven.
"""

from __future__ import annotations

from typing import Any


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


class ProductAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Product",
        label_en="Product",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.product",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),  # v3 products is read-only by design (PR #24325)
    )
    v3_path = "/api/v3/products"
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
            "number": prop(
                "string",
                "Number",
                **RO,
                section="general",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
            ),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=_STATUS_OPTIONS,
                previewable=True,
            ),
            "statusReason": prop("string", "Status reason", **RO, section="general"),
            "kind": prop("select", "Kind", **RO, section="general", options=_KIND_OPTIONS),
            "name": prop(
                "string",
                "Name",
                section="general",
                filterable=True,
                sortable=True,
                searchable=True,
                previewable=True,
            ),
            "description": prop("string", "Description", section="general"),
            "unit": prop("string", "Unit", **RO, section="general"),
            "category": prop(
                "reference",
                "Category",
                # The mapped upstream value is the merchandise group
                # (Warengruppe), not the productsCategories tree.
                reference="MerchandiseGroup",
                renderProperty="name",
                section="general",
            ),
            "project": prop(
                "reference",
                "Project",
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
                    "ean": prop("string", "EAN", **RO, filterable=True, searchable=True),
                    "manufacturerNumber": prop("string", "Manufacturer number", **RO),
                    "hsCode": prop("string", "HS code", **RO),
                    "countryOfOrigin": prop("string", "Country of origin", **RO),
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
                **RO,
                section="general",
                properties={
                    "name": prop("string", "Name", **RO),
                    "website": prop("string", "Website", **RO),
                },
            ),
            "prices": prop(
                "embedded",
                "Prices",
                section="prices",
                properties={
                    "sale": prop(
                        "embedded",
                        "Sale price",
                        **RO,
                        properties={
                            "amount": prop("string", "Amount", **RO),
                            "currency": prop("string", "Currency", **RO),
                        },
                    ),
                    "purchase": prop(
                        "embedded",
                        "Purchase price",
                        **RO,
                        properties={
                            "amount": prop("string", "Amount", **RO),
                            "currency": prop("string", "Currency", **RO),
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
                        **RO,
                        properties={
                            "value": prop("decimal", "Value", **RO),
                            "unit": prop("string", "Unit", **RO),
                        },
                    ),
                    "netWeight": prop(
                        "embedded",
                        "Net weight",
                        **RO,
                        properties={
                            "value": prop("decimal", "Value", **RO),
                            "unit": prop("string", "Unit", **RO),
                        },
                    ),
                    "dimensions": prop(
                        "embedded",
                        "Dimensions",
                        **RO,
                        properties={
                            "length": prop("decimal", "Length", **RO),
                            "width": prop("decimal", "Width", **RO),
                            "height": prop("decimal", "Height", **RO),
                            "unit": prop("string", "Unit", **RO),
                        },
                    ),
                    "minimumOrderQuantity": prop("integer", "Minimum order quantity", **RO),
                    "minimumStockQuantity": prop("integer", "Minimum stock quantity", **RO),
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
                **RO,
                section="tracking",
                properties={
                    "stock": prop("boolean", "Stock item", **RO),
                    "batches": prop("boolean", "Batches", **RO),
                    "serialNumbers": prop("string", "Serial numbers mode", **RO),
                    "bestBefore": prop("boolean", "Best before", **RO),
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
                **RO,
                section="production",
                properties={
                    "mode": prop("select", "Mode", **RO, options=_PRODUCTION_OPTIONS),
                    "hasBillOfMaterials": prop("boolean", "Has bill of materials", **RO),
                },
            ),
            "documentDefaults": prop(
                "embedded",
                "Document defaults",
                section="general",
                properties={
                    "hidePrice": prop("boolean", "Hide price"),
                    "noticeText": prop("string", "Notice text"),
                    "requiresCustomerApproval": prop("boolean", "Requires customer approval"),
                },
            ),
            "variant": prop(
                "embedded",
                "Variant",
                **RO,
                section="variant",
                properties={
                    "of": prop("reference", "Of", reference="Product", renderProperty="name", **RO),
                    "isMatrix": prop("boolean", "Is matrix", **RO),
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
                        "supplier": prop(
                            "reference",
                            "Supplier",
                            reference="Supplier",
                            renderProperty="name",
                            **RO,
                        ),
                        "supplierProductNumber": prop("string", "Supplier product number", **RO),
                        "isDefault": prop("boolean", "Default", **RO),
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
            "tax": {"rate": r.get("salesTax"), "profile": None},
            "logistics": {
                "weight": dim("weight"),
                "netWeight": dim("netWeight"),
                "dimensions": (
                    {
                        "length": (m.get("length") or {}).get("value"),
                        "width": (m.get("width") or {}).get("value"),
                        "height": (m.get("height") or {}).get("value"),
                        "unit": (m.get("length") or {}).get("unit"),
                    }
                    if m.get("length")
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

    # Write (create/update) goes through v2 products upstream (docs/03-mapping),
    # not yet field-verified here — everything is a blue wish until that mapping
    # is built and proven against a live instance.
    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        return {}, {
            k
            for k in model
            if k
            not in {
                "object",
                "id",
                "number",
                "status",
                "statusReason",
                "kind",
                "stock",
                "production",
                "variant",
                "bom",
                "suppliers",
                "identifiers",
                "manufacturer",
                "prices",
                "tracking",
                "createdAt",
                "updatedAt",
            }
        }
