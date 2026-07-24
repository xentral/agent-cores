"""Xentral V3 facade · purchaseOrder — Bestellung (docs/01-model.md §5.1).

Reads Xentral v3 ``/api/v3/purchaseOrders``. Purchase documents carry the partner
as ``address`` (the supplier) + ``supplierNumber``. Confirmation (AB) handling maps
from isConfirmed/confirmationType/supplierOrderNumber. Per ADR-014 only
upstream-writable fields are creatable/updatable; the rest are blue wishes.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import (
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
    manifest = EmulationManifest(
        key="PurchaseOrder",
        label_en="Purchase order",
        category="documents",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.purchaseOrder",
        source_apis=("agentos_neo",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/purchaseOrders"
    include = "lineItems,lineItems.product,project,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
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
                wish="The BF goodsReceipt entity is read-only — no create endpoint (05 #9).",
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
                wish="No public PDF render endpoint; the archived files at /api/v2/{type}/{id}/files are not yet composed.",
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
                    "ourCustomerNumber": prop("string", "Our customer number", filterable=True),
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
                        "taxRate": prop("string", "Tax rate", creatable=True),
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
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
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
    _IGNORE = {
        "dropship",
        "object",
        "id",
        "number",
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
        if i.get("taxRate") is not None:
            out["taxRate"] = i["taxRate"]
        if i.get("supplierProductNumber") is not None:
            out["supplierProductNumber"] = i["supplierProductNumber"]
        if i.get("supplierProductName") is not None:
            out["supplierProductName"] = i["supplierProductName"]
        price = line_price_net(i)
        if price is not None:
            out["price"] = price
        return out
