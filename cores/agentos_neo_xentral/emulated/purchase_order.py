"""Xentral V3 facade · purchaseOrder — Bestellung (docs/01-model.md §5.1).

Reads Xentral v3 ``/api/v3/purchaseOrders``. Purchase documents carry the partner
as ``address`` (the supplier) + ``supplierNumber``. Confirmation (AB) handling maps
from isConfirmed/confirmationType/supplierOrderNumber. Per ADR-014 only
upstream-writable fields are creatable/updatable; the rest are blue wishes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import (
    REQUIRED,
    _TIMEOUT,
    FacadeAdapterBase,
    line_price_net,
    line_qty,
    RO,
    map_tags,
    money,
    prop,
    ref,
    status_map,
    tags_prop,
    tags_to_v3,
)

_STATUS = {
    "draft": "draft",
    "sent": "sent",
    "released": "sent",
    "confirmed": "confirmed",
    "received": "received",
    "completed": "closed",
    "closed": "closed",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("draft", "sent", "confirmed", "received", "closed", "cancelled")
]
_CU = {"creatable": True, "updatable": True}


def _address_props() -> dict[str, Any]:
    s = lambda label: prop("string", label, **_CU)  # noqa: E731
    return {
        "name": s("Name"),
        "street": s("Street"),
        "zip": s("Zip"),
        "city": s("City"),
        "country": s("Country"),
        "email": s("Email"),
        "phone": s("Phone"),
        "vatId": s("VAT id"),
    }


class PurchaseOrderAdapter(FacadeAdapterBase):
    native_search_fields = (
        "number",
        "dates.issued",
        "deliveryAddress.name",
        "deliveryAddress.email",
        "deliveryAddress.zip",
    )
    manifest = EmulationManifest(
        key="PurchaseOrder",
        label_en="Purchase order",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.purchaseOrder",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/v3/purchaseOrders"
    renders_pdf = True
    include = "lineItems,lineItems.product,project,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
        "items.product": "lineItems.product.id",
        "number": "documentNumber",
        "dates.issued": "documentDate",
        "supplier": "address.id",
        "project": "project.id",
        "references.supplierOfferNumber": "supplierOfferNumber",
        "references.ourCustomerNumber": "customerNumber",
        "dates.requestedDelivery": "desiredDeliveryDate",
        "tags": "tags",
    }
    filter_value_maps = {
        "status": {"confirmed": "released", "received": "completed", "closed": "completed"}
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "confirmation": {"label": "Confirmation"},
        "address": {"label": "Address"},
        "items": {"label": "Items"},
        "financials": {"label": "Financials"},
        "flow": {"label": "Document flow"},
    }

    action_map = {
        # v3 exposes both on every business document type.
        "setWriteProtection": ("PATCH", "setWriteProtection"),
        "removeWriteProtection": ("PATCH", "removeWriteProtection"),
        # Release / freigeben from draft (v3 release) — uniform across documents.
        "release": ("PATCH", "release"),
        "close": ("PATCH", "complete"),
        "cancel": ("PATCH", "cancel"),
        "send": ("PATCH", "send"),
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd("release", "Release"),
                    self.step_cmd("close", "Close"),
                    self.step_cmd("cancel", "Cancel"),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "send",
                "Send",
                destructive=True,
                description="Send the purchase order to the supplier (v3 send — mails the document).",
            ),
            self.action_def(
                "recordConfirmation",
                "Record confirmation",
                wish="Supplier order confirmations have no public endpoint.",
            ),
            self.action_def(
                "createGoodsReceipt",
                "Create goods receipt",
                destructive=True,
                description=(
                    "Book the arriving goods against this order (v1 goodsReceipts). This IS "
                    "the posting — there is no separate post step, and unlike the "
                    "StorageLocation actions it has no dryRun: the first real call moves "
                    "stock, and stock movements are append-only. `putaways` says where each "
                    "item lands and carries batch / bestBefore / serial numbers; omit it to "
                    "receive without assigning a location."
                ),
                command={
                    "type": "object",
                    "required": ["date", "items"],
                    "properties": {
                        "date": {
                            "type": "string",
                            "label": "Posting date (YYYY-MM-DD)",
                            "description": (
                                "Required, although the OpenAPI spec marks it optional — "
                                "measured: v1 answers 400 without it. Not defaulted here on "
                                "purpose: the posting date of a stock booking is a decision, "
                                "not a convenience."
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
                                    "orderItem": {
                                        "type": "string",
                                        "label": "Order line this delivers against",
                                    },
                                    "putaways": {
                                        "type": "array",
                                        "label": "Where the quantity is stored",
                                        "items": {
                                            "type": "object",
                                            "required": ["quantity"],
                                            "properties": {
                                                "quantity": {"type": "number", "label": "Quantity"},
                                                "warehouse": {
                                                    "type": "string",
                                                    "label": "Warehouse id (wh_…)",
                                                },
                                                "storageLocation": {
                                                    "type": "string",
                                                    "label": "Storage location id (loc_…)",
                                                },
                                                "batch": {"type": "string", "label": "Batch / lot"},
                                                "bestBefore": {
                                                    "type": "string",
                                                    "label": "Best-before date",
                                                },
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
                },
            ),
            self.action_def(
                "createPurchaseInvoice",
                "Create purchase invoice",
                wish="Creating a supplier invoice from the order is not composed yet (BF supplierInvoice create exists as raw entity CRUD).",
            ),
            self.action_def(
                "requestConfirmation",
                "Request confirmation",
                wish="No public endpoint to request a supplier confirmation.",
            ),
            self.action_def(
                "updateDeliveryDates",
                "Update delivery dates",
                wish="A bulk delivery-date update has no endpoint; per-item dates go through a normal update.",
            ),
            self.action_def(
                "downloadPdf",
                "Download PDF",
                description=(
                    "Fetch the rendered document as a PDF file. Upstream serves the "
                    "archived copy when one exists (written on send and on write "
                    "protection) and renders fresh otherwise. Returns the bytes as "
                    "result.file (base64) — hand it to a file store rather than "
                    "reading it."
                ),
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
                filterable=True,
                previewable=True,
            ),
            "supplier": prop(
                "reference",
                "Supplier",
                **REQUIRED,
                reference="Supplier",
                renderProperty="name",
                section="general",
                creatable=True,
                filterable=True,
                previewable=True,
            ),
            "project": prop(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                section="general",
                **_CU,
            ),
            # costCenter is create-only upstream: v3 keeps it on POST but ignores it
            # on PATCH (a later change does not persist), so it is not `updatable`.
            "costCenter": prop("string", "Cost center", section="general", creatable=True),
            "references": prop(
                "embedded",
                "References",
                section="references",
                properties={
                    "ourCustomerNumber": prop(
                        "string",
                        "Our customer number",
                        description="Not filterable — the upstream list endpoint rejects it (verified on mvp).",
                    ),
                    "supplierOfferNumber": prop("string", "Supplier offer number", filterable=True),
                },
            ),
            "confirmation": prop(
                "embedded",
                "Confirmation",
                **RO,
                section="confirmation",
                properties={
                    "status": prop("select", "Status", **RO),
                    "supplierOrderNumber": prop("string", "Supplier order number", **RO),
                    "confirmedAt": prop("date", "Confirmed at", **RO),
                    "via": prop("string", "Via", **RO),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "issued": prop("date", "Issued", **_CU, filterable=True, sortable=True),
                    "requestedDelivery": prop("date", "Requested delivery", filterable=True),
                    "confirmedDelivery": prop("date", "Confirmed delivery", **RO),
                },
            ),
            "deliveryAddress": prop(
                "embedded", "Delivery address", section="address", properties=_address_props()
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="general",
            ),
            "items": prop(
                "collection",
                "Items",
                section="items",
                node={
                    "properties": {
                        "object": prop("string", "Object", **RO),
                        "id": prop("string", "Item id", **RO),
                        "position": prop("integer", "Position"),
                        "product": prop(
                            "reference",
                            "Product",
                            **REQUIRED,
                            reference="Product",
                            renderProperty="name",
                            creatable=True,
                            filterable=True,
                        ),
                        "supplierProductNumber": prop(
                            "string", "Supplier product number", creatable=True
                        ),
                        "supplierProductName": prop(
                            "string", "Supplier product name", creatable=True
                        ),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            **REQUIRED,
                            creatable=True,
                            properties={
                                "value": prop("decimal", "Value"),
                                "unit": prop("string", "Unit"),
                            },
                        ),
                        "unitPrice": prop(
                            "embedded",
                            "Unit price",
                            creatable=True,
                            properties={
                                "amount": prop("decimal", "Amount"),
                                "currency": prop("string", "Currency"),
                            },
                        ),
                        # Derived upstream from the product's tax class and the customer's tax rule.
                        # v3 accepts taxRate on a line, answers 2xx and keeps its own value — measured
                        # on offers, salesOrders, invoices and purchaseOrders (2026-08-01): sent
                        # "reduced", read back "standard", both on create and on update. Declaring it
                        # writable promised an edit that silently did nothing.
                        "taxRate": prop("string", "Tax rate", **RO),
                        "fulfillment": prop(
                            "embedded",
                            "Fulfillment",
                            **RO,
                            properties={
                                "received": prop("decimal", "Received", **RO),
                                "invoiced": prop("decimal", "Invoiced", **RO),
                            },
                        ),
                    }
                },
            ),
            # Currency is derived from the supplier/config and surfaces only in
            # totals.*.currency — v3 has no writable currency on the PO (writes to
            # financials.currency were silently dropped), so it is read-only.
            "currency": prop("string", "Currency", section="financials", **RO),
            "totals": prop(
                "embedded",
                "Totals",
                **RO,
                section="financials",
                properties={
                    "currency": prop("string", "Currency", **RO),
                    "net": prop("string", "Net", **RO),
                    "gross": prop("string", "Gross", **RO),
                },
            ),
            "payment": prop(
                "embedded",
                "Payment",
                section="financials",
                properties={
                    "method": prop(
                        "reference", "Method", reference="PaymentMethod", renderProperty="name"
                    ),
                    "terms": prop(
                        "embedded",
                        "Terms",
                        properties={
                            "dueDays": prop("integer", "Due days"),
                            "discountPercent": prop("decimal", "Discount %"),
                            "discountDays": prop("integer", "Discount days"),
                        },
                    ),
                },
            ),
            "note": prop("string", "Note", section="general", **_CU),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "goodsReceipts": prop(
                        "collection",
                        "Goods receipts",
                        **RO,
                        node={
                            "properties": {
                                "id": prop("string", "ID", **RO),
                                "number": prop("string", "Number", **RO),
                            }
                        },
                    ),
                    "purchaseInvoices": prop(
                        "collection",
                        "Purchase invoices",
                        **RO,
                        node={
                            "properties": {
                                "id": prop("string", "ID", **RO),
                                "number": prop("string", "Number", **RO),
                            }
                        },
                    ),
                },
            ),
            "tags": tags_prop(writable=True),
            # v3 exposes it on every business document (BusinessDocumentResource:
            # `writeProtection => isWriteProtected()`) and filters on it. Flip it with
            # the setWriteProtection / removeWriteProtection actions — a protected
            # document refuses every update until it is released.
            "writeProtection": prop("boolean", "Write protection", **RO, filterable=True),
            "printSettings": prop(
                "embedded",
                "Print settings",
                section="general",
                properties={
                    "withoutPrices": prop(
                        "boolean", "Without prices", creatable=True, updatable=True
                    ),
                    "requestConfirmation": prop(
                        "boolean", "Request confirmation", creatable=True, updatable=True
                    ),
                },
            ),
            "dropship": prop("embedded", "Dropship", **RO, section="general", properties={}),
            "customFields": prop("embedded", "Custom fields", section="general", properties={}),
            "createdAt": prop(
                "datetime",
                "Created at",
                **RO,
                sortable=True,
                filterable=True,
                description="When the record was created. Filterable.",
            ),
            "updatedAt": prop(
                "datetime",
                "Updated at",
                **RO,
                sortable=True,
                filterable=True,
                description=(
                    "When the record last changed. Filterable — this is the key for "
                    "an incremental sync: ask for what changed since the last run "
                    "instead of paging the whole collection."
                ),
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        def addr(a: dict[str, Any] | None, vat: Any = None) -> dict[str, Any] | None:
            if not isinstance(a, dict):
                return None
            return {
                "name": a.get("name"),
                "street": a.get("street"),
                "zip": a.get("zipCode"),
                "city": a.get("city"),
                "country": a.get("country"),
                "email": a.get("email"),
                "phone": a.get("phone"),
                "vatId": vat,
            }

        fin = r.get("financials") or {}
        tot = r.get("totals") or {}
        cur = (
            fin.get("currency")
            or (tot.get("net") or {}).get("currency")
            or (tot.get("gross") or {}).get("currency")
            or "EUR"
        )
        terms = fin.get("paymentTerms") or {}
        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            price = (li.get("price") or {}).get("net") or {}
            items.append(
                {
                    "object": "purchaseOrderItem",
                    "id": str(li.get("id")) if li.get("id") else None,
                    "position": li.get("order"),
                    "product": ref(
                        "prd_", p.get("id"), p.get("number"), li.get("name"), "products"
                    ),
                    "supplierProductNumber": li.get("supplierProductNumber"),
                    "supplierProductName": li.get("supplierProductName"),
                    "quantity": {"value": li.get("quantity"), "unit": li.get("unit") or "piece"},
                    "unitPrice": money(price.get("amount"), price.get("currency") or cur),
                    "taxRate": li.get("taxRate"),
                    "fulfillment": {"received": li.get("deliveredQuantity"), "invoiced": None},
                }
            )
        conf_status = "confirmed" if r.get("isConfirmed") else "pending"
        return {
            "object": "purchaseOrder",
            "id": (f"po_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "supplier": ref(
                "sup_",
                (r.get("address") or {}).get("id"),
                r.get("supplierNumber"),
                (r.get("documentAddress") or {}).get("name"),
                "suppliers",
            ),
            "project": ref(
                "prj_",
                (r.get("project") or {}).get("id"),
                None,
                (r.get("project") or {}).get("name"),
                "projects",
            ),
            "costCenter": r.get("costCenter"),
            "references": {
                "ourCustomerNumber": r.get("customerNumber"),
                "supplierOfferNumber": r.get("supplierOfferNumber"),
            },
            "confirmation": {
                "status": conf_status,
                "supplierOrderNumber": r.get("supplierOrderNumber"),
                "confirmedAt": None,
                "via": r.get("confirmationType"),
            },
            "dates": {
                "issued": r.get("documentDate"),
                "requestedDelivery": r.get("desiredDeliveryDate"),
                "confirmedDelivery": r.get("confirmedDeliveryDate"),
            },
            "deliveryAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "warehouse": None,
            "items": items,
            "currency": cur,
            "totals": {
                "currency": cur,
                "net": (money((tot.get("net") or {}).get("amount"), cur) or {}).get("amount"),
                "gross": (money((tot.get("gross") or {}).get("amount"), cur) or {}).get("amount"),
            },
            "payment": {
                "method": ref(
                    "paym_",
                    (fin.get("paymentMethod") or {}).get("id"),
                    None,
                    None,
                    "paymentMethods",
                ),
                "terms": {
                    "dueDays": terms.get("paymentTargetDays"),
                    "discountPercent": terms.get("paymentTargetDiscount"),
                    "discountDays": terms.get("paymentTargetDiscountDays"),
                },
            },
            "note": r.get("internalComment"),
            "documents": {"goodsReceipts": [], "purchaseInvoices": []},
            "tags": map_tags(r.get("tags")),
            "writeProtection": r.get("writeProtection"),
            "customFields": r.get("customFields") or {},
            "printSettings": {
                "withoutPrices": (r.get("printSettings") or {}).get("withoutPrices"),
                "requestConfirmation": (r.get("printSettings") or {}).get(
                    "isConfirmationRequestVisible"
                ),
            },
            "dropship": None,
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    _WRITABLE = {
        "printSettings",
        "supplier",
        "project",
        "costCenter",
        "note",
        "deliveryAddress",
        "items",
        "dates",
        "tags",
    }
    # `number` is deliberately NOT ignored: a document number always comes from the
    # configured number range, so a caller supplying one must be told it was refused
    # rather than get a 201 and a different number. Upstream would accept it on three
    # of these types (salesOrder / invoice / creditNote, verified on mvp) — declining
    # it everywhere is a product decision, recorded as such in priorities.json.
    _IGNORE = {
        "dropship",
        "object",
        "id",
        "status",
        "confirmation",
        "warehouse",
        "totals",
        "documents",
        "createdAt",
        "updatedAt",
    }

    @staticmethod
    def _ref_id(v: Any) -> dict[str, Any] | None:
        if isinstance(v, dict):
            ident = v.get("id") or v.get("number")
            return (
                {"id": str(ident).split("_", 1)[1] if "_" in str(ident) else str(ident)}
                if ident
                else None
            )
        return (
            {"id": str(v).split("_", 1)[1] if "_" in str(v) else str(v)}
            if v not in (None, "")
            else None
        )

    @staticmethod
    def _addr_to_v3(a: dict[str, Any] | None) -> dict[str, Any]:
        a = a or {}
        return {
            k: a.get(src)
            for k, src in (
                ("name", "name"),
                ("street", "street"),
                ("zipCode", "zip"),
                ("city", "city"),
                ("country", "country"),
                ("email", "email"),
                ("phone", "phone"),
            )
            if a.get(src) is not None
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        v3: dict[str, Any] = {}
        rejected: set[str] = set()
        if "project" in model:
            v3["project"] = self._ref_id(model["project"])
        if "costCenter" in model:
            v3["costCenter"] = model["costCenter"]
        if "note" in model:
            v3["internalComment"] = model["note"]
        if "deliveryAddress" in model:
            v3["documentAddress"] = self._addr_to_v3(model["deliveryAddress"])
            if (model["deliveryAddress"] or {}).get("vatId"):
                v3["vatId"] = model["deliveryAddress"]["vatId"]
        if "dates" in model and (model["dates"] or {}).get("issued"):
            v3["documentDate"] = model["dates"]["issued"]
        if "supplier" in model:
            if creating:
                v3["address"] = self._ref_id(model["supplier"])
            else:
                rejected.add("supplier")
        if "items" in model:
            if creating:
                v3["lineItems"] = [
                    self._item_to_v3(i) for i in model["items"] if isinstance(i, dict)
                ]
            else:
                rejected.add("items")
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        if "printSettings" in model and isinstance(model["printSettings"], dict):
            ps = model["printSettings"]
            v3["printSettings"] = {
                k: v
                for k, v in (
                    ("withoutPrices", ps.get("withoutPrices")),
                    ("isConfirmationRequestVisible", ps.get("requestConfirmation")),
                )
                if v is not None
            }
        for k in model:
            if k in self._WRITABLE or k in self._IGNORE:
                continue
            rejected.add(k)
        return v3, rejected

    @staticmethod
    def _item_to_v3(i: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        prod = i.get("product")
        if prod is not None:
            pid = prod.get("id") if isinstance(prod, dict) else prod
            out["product"] = {"id": str(pid).split("_", 1)[1] if "_" in str(pid) else str(pid)}
        qty_val = line_qty(i)
        if qty_val is not None:
            out["quantity"] = qty_val
        if i.get("discountPercent") is not None:
            out["discount"] = i["discountPercent"]
        if i.get("supplierProductNumber") is not None:
            out["supplierProductNumber"] = i["supplierProductNumber"]
        if i.get("supplierProductName") is not None:
            out["supplierProductName"] = i["supplierProductName"]
        price = line_price_net(i)
        if price is not None:
            out["price"] = price
        return out

    # ---- goods receipt -----------------------------------------------------
    async def action(  # noqa: ANN001
        self, *, action_key, handle, body, base_url, token, accept_language=None, client=None
    ):
        if action_key == "createGoodsReceipt":
            return await self._create_goods_receipt(
                handle, body, base_url, token, accept_language, client
            )
        return await super().action(
            action_key=action_key,
            handle=handle,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )

    async def _create_goods_receipt(  # noqa: ANN001
        self, handle, body, base_url, token, accept_language, client
    ):
        """POST /api/v1/purchaseOrders/{id}/goodsReceipts — receive and BOOK.

        The model's vocabulary is translated onto v1's: ``items`` → ``positions``,
        ``orderItem`` → ``purchaseOrderPosition``, ``putaways`` → ``stockMovements``,
        and the flat ``batch``/``bestBefore``/``serialNumbers`` back into upstream's
        nested ``qualityControlAttributes``. `putaway` is the core's own word for
        booking stock onto a location (StorageLocation.putaway), so a goods receipt
        uses it too rather than importing v1's generic ``stockMovements``.
        """
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        command = envelope.get("command") or {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        order = self._ref_id(ids[0] if ids else None)
        if order is None:
            return self._refuse(422, "createGoodsReceipt needs the purchase order id")

        if not command.get("date"):
            return self._refuse(
                422,
                "createGoodsReceipt needs command.date (YYYY-MM-DD) — upstream rejects a "
                "receipt without a posting date",
            )
        items = command.get("items")
        if not isinstance(items, list) or not items:
            return self._json(
                422,
                {
                    "title": (
                        "createGoodsReceipt needs command.date and command.items="
                        "[{product, quantity, orderItem?, putaways?}]"
                    )
                },
            )

        positions: list[dict[str, Any]] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            product = self._ref_id(m.get("product"))
            if product is None:
                return self._refuse(422, f"createGoodsReceipt: missing product in {m}")
            try:
                qty = float(m.get("quantity"))
            except (TypeError, ValueError):
                return self._refuse(422, f"createGoodsReceipt: bad quantity in {m}")
            if qty <= 0:
                return self._refuse(422, f"createGoodsReceipt: quantity must be > 0 in {m}")
            pos: dict[str, Any] = {"product": product, "quantity": qty}
            order_item = self._ref_id(m.get("orderItem"))
            if order_item is not None:
                pos["purchaseOrderPosition"] = order_item

            movements: list[dict[str, Any]] = []
            for p in m.get("putaways") or []:
                if not isinstance(p, dict):
                    continue
                try:
                    pqty = float(p.get("quantity", qty))
                except (TypeError, ValueError):
                    return self._refuse(422, f"createGoodsReceipt: bad putaway quantity in {p}")
                mv: dict[str, Any] = {"quantity": pqty}
                for model_key, up_key in (
                    ("warehouse", "warehouse"),
                    ("storageLocation", "storageLocation"),
                ):
                    target = self._ref_id(p.get(model_key))
                    if target is not None:
                        mv[up_key] = target
                # Flat in the model, nested upstream — the model has no
                # "quality control" concept, it has batches and serial numbers.
                qc: dict[str, Any] = {}
                if p.get("batch") is not None:
                    qc["batch"] = p["batch"]
                if p.get("bestBefore") is not None:
                    qc["bestBeforeDate"] = p["bestBefore"]
                serials = [s for s in (p.get("serialNumbers") or []) if s]
                if serials:
                    qc["serialNumbers"] = [
                        {"number": s.get("number") if isinstance(s, dict) else str(s)}
                        for s in serials
                    ]
                if qc:
                    mv["qualityControlAttributes"] = qc
                movements.append(mv)
            if movements:
                pos["stockMovements"] = movements
            positions.append(pos)

        payload: dict[str, Any] = {"date": command["date"], "positions": positions}

        url = f"{base_url.rstrip('/')}/api/v1/purchaseOrders/{order['id']}/goodsReceipts"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001, ANN202
            return await c.post(url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            rbody = resp.json()
        except ValueError:
            rbody = {}
        if resp.status_code >= 400:
            return self._json(
                resp.status_code,
                rbody if isinstance(rbody, dict) else {"title": "createGoodsReceipt failed"},
            )
        # v1 answers 201 with an empty body and a Location header; report the id so a
        # workflow can read the receipt back instead of having to search for it.
        location = resp.headers.get("Location") or ""
        created = location.rstrip("/").rsplit("/", 1)[-1] if location else None
        return self._json(
            201,
            {"data": {"object": "goodsReceipt", "id": f"gr_{created}" if created else None}},
        )
