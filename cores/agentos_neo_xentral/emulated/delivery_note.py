"""Xentral V3 facade · deliveryNote — Lieferschein (docs/01-model.md §4.3).

Reads Xentral v3 ``/api/v3/deliveryNotes`` and maps into the new model. Shipments
(tracking) need a nachlade GET ``/v1/deliveryNotes/{id}/shipments`` — deferred, so
``shipments`` is empty for now. Per ADR-014 only upstream-writable fields are
creatable/updatable; the rest are blue wishes.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import (
    FacadeAdapterBase,
    RO,
    line_qty,
    map_tags,
    prop,
    ref,
    status_map,
    tags_prop,
    tags_to_v3,
)

_STATUS = {
    "draft": "draft",
    "released": "picking",
    "picking": "picking",
    "shipped": "shipped",
    "delivered": "delivered",
    "completed": "delivered",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("draft", "picking", "shipped", "delivered", "cancelled")
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


def _item_props() -> dict[str, Any]:
    return {
        "object": prop("string", "Object", **RO),
        "id": prop("string", "Item id", **RO),
        "position": prop("integer", "Position"),
        "orderItem": prop(
            "reference", "Order item", reference="SalesOrder", renderProperty="number", **RO
        ),
        "product": prop(
            "reference",
            "Product",
            reference="Product",
            renderProperty="name",
            creatable=True,
            filterable=True,
        ),
        "description": prop("string", "Description", creatable=True),
        "quantity": prop(
            "embedded",
            "Quantity",
            creatable=True,
            properties={"value": prop("decimal", "Value"), "unit": prop("string", "Unit")},
        ),
        "deliveredQuantity": prop("decimal", "Delivered quantity", **RO),
        "batches": prop(
            "collection",
            "Batches",
            **RO,
            node={
                "properties": {
                    "batch": prop(
                        "reference", "Batch", reference="Batch", renderProperty="number", **RO
                    ),
                    "quantity": prop("decimal", "Quantity", **RO),
                }
            },
        ),
        "serialNumbers": prop(
            "collection",
            "Serial numbers",
            **RO,
            node={"properties": {"number": prop("string", "Number", **RO)}},
        ),
    }


class DeliveryNoteAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="DeliveryNote",
        label_en="Delivery note",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.deliveryNote",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/deliveryNotes"
    include = "lineItems,lineItems.product,project,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
        "number": "documentNumber",
        "dates.issued": "documentDate",
        "customer": "address.id",
        "project": "project.id",
        "references.customerOrderNumber": "customerOrderNumber",
        "tags": "tags",
    }
    filter_value_maps = {
        "status": {"picking": "released", "shipped": "completed", "delivered": "completed"}
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "address": {"label": "Address"},
        "items": {"label": "Items"},
        "shipping": {"label": "Shipping"},
        "flow": {"label": "Document flow"},
    }

    action_map = {
        # Release / freigeben from draft (v3 release) — uniform across documents.
        "release": ("PATCH", "release"),
        "markDelivered": ("PATCH", "complete"),
        "cancel": ("PATCH", "cancel"),
        "createSalesInvoice": {
            "method": "POST",
            "path": "/api/v3/invoices/actions/createFromDeliveryNote",
            "body": {"deliveryNote": {"id": "{id}"}},
        },
        "createReturn": {
            "method": "POST",
            "path": "/api/v3/returnOrders/actions/createFromDeliveryNote",
            "body": {"deliveryNote": {"id": "{id}"}},
        },
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd("release", "Release"),
                    self.step_cmd(
                        "startPicking",
                        "Start picking",
                        wish="Picking is driven by picking runs — no direct startPicking endpoint.",
                    ),
                    self.step_cmd("markDelivered", "Mark delivered"),
                    self.step_cmd("cancel", "Cancel"),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "createShipment",
                "Create shipment",
                wish="The carrier label API (printShippingLabel) is beta and not public.",
            ),
            self.action_def(
                "createReturn",
                "Create return",
                description="Creates a return for items of this delivery note (v3 returnOrders createFromDeliveryNote). The command must carry lineItems [{id, quantity, returnReason: {id}}].",
                command={
                    "type": "object",
                    "required": ["lineItems"],
                    "properties": {
                        "lineItems": {
                            "type": "array",
                            "label": "Line items ({id, quantity, returnReason:{id}})",
                        }
                    },
                },
            ),
            self.action_def(
                "createSalesInvoice",
                "Create sales invoice",
                description="Creates the invoice from this delivery note (v3 invoices createFromDeliveryNote).",
            ),
            self.action_def(
                "downloadPdf",
                "Download PDF",
                wish="No public PDF render endpoint; the archived files at /api/v2/{type}/{id}/files are not yet composed.",
            ),
            self.action_def(
                "printLabels",
                "Print labels",
                wish="Per-product label print exists (v1 products/{id}/printLabel); a per-delivery-note batch print is not composed.",
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
            "customer": prop(
                "reference",
                "Customer",
                reference="Customer",
                renderProperty="name",
                section="general",
                creatable=True,
                filterable=True,
                previewable=True,
            ),
            "channel": prop(
                "reference",
                "Channel",
                reference="Channel",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            "project": prop(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                section="general",
                **_CU,
            ),
            "costCenter": prop("string", "Cost center", section="general", **_CU),
            "references": prop(
                "embedded",
                "References",
                section="references",
                properties={
                    "customerOrderNumber": prop(
                        "string", "Customer order number", **_CU, filterable=True
                    )
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "issued": prop("date", "Issued", **_CU, filterable=True, sortable=True),
                    "shipped": prop("date", "Shipped", **RO),
                    "delivered": prop("date", "Delivered", **RO),
                },
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="shipping",
            ),
            "billingAddress": prop(
                "embedded", "Address", section="address", properties=_address_props()
            ),
            "items": prop(
                "collection", "Items", section="items", node={"properties": _item_props()}
            ),
            "shipments": prop(
                "collection",
                "Shipments",
                **RO,
                section="shipping",
                node={
                    "properties": {
                        "id": prop("string", "ID", **RO),
                        "number": prop("string", "Tracking", **RO),
                        "name": prop("string", "Name", **RO),
                    }
                },
            ),
            "customs": prop(
                "embedded",
                "Customs",
                section="shipping",
                properties={
                    "totalWeight": prop(
                        "embedded",
                        "Total weight",
                        properties={
                            "value": prop("decimal", "Value"),
                            "unit": prop("string", "Unit"),
                        },
                    ),
                    "incoterm": prop("string", "Incoterm"),
                    "note": prop("string", "Note"),
                },
            ),
            "note": prop("string", "Note", section="general", **_CU),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "salesOrder": prop(
                        "reference",
                        "Sales order",
                        reference="SalesOrder",
                        renderProperty="number",
                        **RO,
                    ),
                    "salesInvoices": prop(
                        "collection",
                        "Sales invoices",
                        **RO,
                        node={
                            "properties": {
                                "id": prop("string", "ID", **RO),
                                "number": prop("string", "Number", **RO),
                            }
                        },
                    ),
                    "returns": prop(
                        "collection",
                        "Returns",
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

        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            soli = li.get("salesOrderLineItem") or {}
            items.append(
                {
                    "object": "deliveryNoteItem",
                    "id": str(li.get("id")) if li.get("id") else None,
                    "position": li.get("order"),
                    "orderItem": ref("itm_", soli.get("id"), None, None, "salesOrders")
                    if soli.get("id")
                    else None,
                    "product": ref(
                        "prd_", p.get("id"), p.get("number"), li.get("name"), "products"
                    ),
                    "description": li.get("description"),
                    "quantity": {"value": li.get("quantity"), "unit": li.get("unit") or "piece"},
                    "deliveredQuantity": li.get("deliveredQuantity"),
                    "batches": [],
                    "serialNumbers": [],
                }
            )

        so = r.get("salesOrder")
        inv = r.get("invoice")
        inv_ref = ref(
            "si_", inv.get("id") if isinstance(inv, dict) else inv, None, None, "salesInvoices"
        )

        return {
            "object": "deliveryNote",
            "id": (f"dn_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "customer": ref(
                "cus_",
                (r.get("address") or {}).get("id"),
                r.get("customerNumber"),
                (r.get("documentAddress") or {}).get("name"),
                "customers",
            ),
            "channel": None,
            "project": ref(
                "prj_",
                (r.get("project") or {}).get("id"),
                None,
                (r.get("project") or {}).get("name"),
                "projects",
            ),
            "costCenter": r.get("costCenter"),
            "references": {"customerOrderNumber": r.get("customerOrderNumber")},
            "dates": {"issued": r.get("documentDate"), "shipped": None, "delivered": None},
            "warehouse": ref(
                "wh_",
                (r.get("preferredWarehouse") or {}).get("id"),
                None,
                (r.get("preferredWarehouse") or {}).get("name"),
                "warehouses",
            ),
            "billingAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "items": items,
            "shipments": [],
            "customs": {"totalWeight": None, "incoterm": None, "note": None},
            "note": r.get("internalComment"),
            "documents": {
                "salesOrder": ref(
                    "so_", so.get("id") if isinstance(so, dict) else so, None, None, "salesOrders"
                ),
                "salesInvoices": [inv_ref] if inv_ref else [],
                "returns": [],
            },
            "tags": map_tags(r.get("tags")),
            "customFields": r.get("customFields") or {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # ---- write -----------------------------------------------------------
    _WRITABLE = {
        "customer",
        "project",
        "costCenter",
        "note",
        "billingAddress",
        "items",
        "dates",
        "tags",
        "references",
    }
    _IGNORE = {
        "object",
        "id",
        "number",
        "status",
        "warehouse",
        "shipments",
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
        if "billingAddress" in model:
            v3["documentAddress"] = self._addr_to_v3(model["billingAddress"])
            if (model["billingAddress"] or {}).get("vatId"):
                v3["vatId"] = model["billingAddress"]["vatId"]
        if "dates" in model:
            d = model["dates"] or {}
            if d.get("issued"):
                v3["documentDate"] = d["issued"]
        if "customer" in model:
            if creating:
                v3["address"] = self._ref_id(model["customer"])
            else:
                rejected.add("customer")
        if "items" in model:
            if creating:
                v3["lineItems"] = [
                    self._item_to_v3(i) for i in model["items"] if isinstance(i, dict)
                ]
            else:
                rejected.add("items")
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        if "references" in model:
            refs = model["references"] or {}
            if "customerOrderNumber" in refs:  # v3 customerOrderNumber (API-731 parity)
                v3["customerOrderNumber"] = refs["customerOrderNumber"]
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
        if i.get("description") is not None:
            out["description"] = i["description"]
        if i.get("discountPercent") is not None:
            out["discount"] = i["discountPercent"]
        if i.get("taxRate") is not None:
            out["taxRate"] = i["taxRate"]
        return out
