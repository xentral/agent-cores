"""Xentral V3 facade · return — Retoure (docs/01-model.md §4.6).

Reads Xentral v3 ``/api/v3/returnOrders``. The new status chain
(requested→received→checked→settled) maps from the upstream ``progress`` field
(``status`` only gates cancelled). Per-item condition/action are not in the v3
payload yet (blue wishes). Per ADR-014 only upstream-writable fields are
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

_PROGRESS = {
    "announced": "requested",
    "received": "received",
    "checked": "checked",
    "booked": "settled",
    "settled": "settled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("requested", "received", "checked", "settled", "cancelled")
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


class ReturnAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Return",
        label_en="Return",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.return",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/returnOrders"
    include = "lineItems,lineItems.product,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
        "number": "documentNumber",
        "dates.requested": "documentDate",
        "customer": "address.id",
        "project": "project.id",
        "references.customerOrderNumber": "customerOrderNumber",
        "tags": "tags",
    }
    filter_value_maps = {
        "status": {"requested": "released", "checked": "completed", "settled": "completed"}
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "address": {"label": "Address"},
        "items": {"label": "Items"},
        "flow": {"label": "Document flow"},
    }

    action_map = {
        "settle": ("PATCH", "complete"),
        "cancel": ("PATCH", "cancel"),
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd(
                        "receive",
                        "Receive",
                        wish="Receiving has no upstream endpoint — v3 returnOrders offers complete/cancel only.",
                    ),
                    self.step_cmd(
                        "check", "Check", wish="The check step has no upstream endpoint."
                    ),
                    self.step_cmd("settle", "Settle"),
                    self.step_cmd("cancel", "Cancel"),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "sendReturnLabel",
                "Send return label",
                wish="Return labels run through the beta carrier label API — not public.",
            ),
            self.action_def(
                "createCreditNote",
                "Create credit note",
                wish="No createFrom endpoint for credit notes upstream.",
            ),
            self.action_def(
                "createReplacementOrder",
                "Create replacement order",
                wish="No replacement-order endpoint upstream.",
            ),
            self.action_def(
                "restock",
                "Restock",
                wish="Restocking runs through goods receipt — no direct endpoint.",
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
            "project": prop(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                section="general",
                **_CU,
            ),
            "references": prop(
                "embedded",
                "References",
                section="references",
                properties={
                    "rmaNumber": prop("string", "RMA number", filterable=True),
                    "customerOrderNumber": prop("string", "Customer order number", filterable=True),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "requested": prop("date", "Requested", **_CU, filterable=True, sortable=True),
                    "received": prop("date", "Received", **RO),
                    "settled": prop("date", "Settled", **RO),
                },
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="general",
            ),
            "billingAddress": prop(
                "embedded", "Address", section="address", properties=_address_props()
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
                        "deliveryNoteItem": prop(
                            "reference",
                            "Delivery note item",
                            reference="DeliveryNote",
                            renderProperty="number",
                            **RO,
                        ),
                        "product": prop(
                            "reference",
                            "Product",
                            reference="Product",
                            renderProperty="name",
                            creatable=True,
                            filterable=True,
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
                        "reason": prop(
                            "reference",
                            "Reason",
                            reference="ReturnReason",
                            renderProperty="name",
                            creatable=True,
                            required=True,
                        ),
                        "condition": prop("select", "Condition"),
                        "action": prop("select", "Action"),
                        "receivedQuantity": prop("decimal", "Received quantity", **RO),
                        "creditedQuantity": prop("decimal", "Credited quantity", **RO),
                    }
                },
            ),
            "resolution": prop(
                "embedded",
                "Resolution",
                **RO,
                section="flow",
                properties={
                    "creditNote": prop(
                        "reference",
                        "Credit note",
                        reference="CreditNote",
                        renderProperty="number",
                        **RO,
                    ),
                    "replacementOrder": prop(
                        "reference",
                        "Replacement order",
                        reference="SalesOrder",
                        renderProperty="number",
                        **RO,
                    ),
                },
            ),
            "note": prop("string", "Note", section="general", **_CU),
            "documents": prop(
                "embedded",
                "Documents",
                section="flow",
                properties={
                    # Create-only: link the return to its source order / delivery note
                    # (v3 salesOrder{id} / deliveryNote{id}). On read they show the
                    # linked documents.
                    "salesOrder": prop(
                        "reference",
                        "Sales order",
                        reference="SalesOrder",
                        renderProperty="number",
                        creatable=True,
                    ),
                    "deliveryNote": prop(
                        "reference",
                        "Delivery note",
                        reference="DeliveryNote",
                        renderProperty="number",
                        creatable=True,
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

        status = (
            "cancelled"
            if r.get("status") == "cancelled"
            else status_map(_PROGRESS, r.get("progress"), "requested")
        )
        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            dnli = li.get("deliveryNoteLineItem") or {}
            reason = li.get("returnReason") or {}
            items.append(
                {
                    "object": "returnItem",
                    "id": str(li.get("id")) if li.get("id") else None,
                    "position": li.get("order"),
                    "deliveryNoteItem": ref("itm_", dnli.get("id"), None, None, "deliveryNotes")
                    if dnli.get("id")
                    else None,
                    "product": ref(
                        "prd_", p.get("id"), p.get("number"), li.get("name"), "products"
                    ),
                    "quantity": {"value": li.get("quantity"), "unit": li.get("unit") or "piece"},
                    "reason": ref(
                        "rsn_", reason.get("id"), None, reason.get("name"), "returnReasons"
                    )
                    if reason.get("id")
                    else None,
                    "condition": None,
                    "action": None,
                    "receivedQuantity": li.get("receivedQuantity"),
                    "creditedQuantity": li.get("reimbursementQuantity"),
                }
            )
        so, dn = r.get("salesOrder"), r.get("deliveryNote")
        cn, rso = r.get("creditNote"), r.get("replacementSalesOrder")
        return {
            "object": "return",
            "id": (f"ret_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status,
            "customer": ref(
                "cus_",
                (r.get("address") or {}).get("id"),
                r.get("customerNumber"),
                (r.get("documentAddress") or {}).get("name"),
                "customers",
            ),
            "project": ref(
                "prj_",
                (r.get("project") or {}).get("id"),
                None,
                (r.get("project") or {}).get("name"),
                "projects",
            ),
            "references": {"rmaNumber": None, "customerOrderNumber": r.get("customerOrderNumber")},
            "dates": {"requested": r.get("documentDate"), "received": None, "settled": None},
            "warehouse": ref(
                "wh_",
                (r.get("preferredWarehouse") or {}).get("id"),
                None,
                (r.get("preferredWarehouse") or {}).get("name"),
                "warehouses",
            ),
            "billingAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "items": items,
            "resolution": {
                "creditNote": ref(
                    "cn_", cn.get("id") if isinstance(cn, dict) else cn, None, None, "creditNotes"
                ),
                "replacementOrder": ref(
                    "so_",
                    rso.get("id") if isinstance(rso, dict) else rso,
                    None,
                    None,
                    "salesOrders",
                ),
            },
            "note": r.get("internalComment"),
            "documents": {
                "salesOrder": ref(
                    "so_", so.get("id") if isinstance(so, dict) else so, None, None, "salesOrders"
                ),
                "deliveryNote": ref(
                    "dn_", dn.get("id") if isinstance(dn, dict) else dn, None, None, "deliveryNotes"
                ),
            },
            "tags": map_tags(r.get("tags")),
            "customFields": r.get("customFields") or {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    _WRITABLE = {
        "customer",
        "project",
        "note",
        "billingAddress",
        "items",
        "dates",
        "tags",
        "documents",
    }
    _IGNORE = {
        "object",
        "id",
        "number",
        "status",
        "warehouse",
        "resolution",
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
        if "note" in model:
            v3["internalComment"] = model["note"]
        if "billingAddress" in model:
            v3["documentAddress"] = self._addr_to_v3(model["billingAddress"])
            if (model["billingAddress"] or {}).get("vatId"):
                v3["vatId"] = model["billingAddress"]["vatId"]
        if "dates" in model and (model["dates"] or {}).get("requested"):
            v3["documentDate"] = model["dates"]["requested"]
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
        if "documents" in model:
            # Link the return to its source order / delivery note (create-only).
            if creating:
                docs = model["documents"] or {}
                so = self._ref_id(docs.get("salesOrder")) if isinstance(docs, dict) else None
                if so is not None:
                    v3["salesOrder"] = so
                dn = self._ref_id(docs.get("deliveryNote")) if isinstance(docs, dict) else None
                if dn is not None:
                    v3["deliveryNote"] = dn
            else:
                rejected.add("documents")
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
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
        reason = i.get("reason")
        if reason is not None:
            rid = reason.get("id") if isinstance(reason, dict) else reason
            if rid not in (None, ""):
                out["returnReason"] = {
                    "id": str(rid).split("_", 1)[1] if "_" in str(rid) else str(rid)
                }
        return out
