"""Xentral V3 facade · creditNote — Gutschrift (docs/01-model.md §4.5).

Reads Xentral v3 ``/api/v3/creditNotes``. ``kind`` (correction | cancellation)
derives from the upstream ``isCancellationInvoice`` flag. Settlement/refund status
beyond paymentStatus is a gap (docs/05 #2) — best-effort. Per ADR-014 only
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
    "released": "open",
    "sent": "open",
    "open": "open",
    "completed": "settled",
    "paid": "settled",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("draft", "open", "settled", "cancelled")
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


class CreditNoteAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="CreditNote",
        label_en="Credit note",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.creditNote",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/creditNotes"
    include = "lineItems,lineItems.product,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
        "number": "documentNumber",
        "dates.issued": "documentDate",
        "customer": "address.id",
        "project": "project.id",
        "references.customerOrderNumber": "customerOrderNumber",
        "tags": "tags",
        "references.debtorAccountNumber": "deviatingDebtorAccountNumber",
        "dates.serviceDate": "deliveryDate",
    }
    filter_value_maps = {"status": {"open": "released", "settled": "completed"}}
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "address": {"label": "Address"},
        "items": {"label": "Items"},
        "financials": {"label": "Financials"},
        "flow": {"label": "Document flow"},
    }

    action_map = {
        "issue": ("PATCH", "release"),
        "cancel": ("PATCH", "cancel"),
        "send": ("PATCH", "send"),
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd("issue", "Issue"),
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
                description="Send the credit note to the customer (v3 send — mails the document).",
            ),
            self.action_def(
                "registerRefund",
                "Register refund",
                wish="The payments API is not public — no endpoint to register an outgoing refund.",
            ),
            self.action_def(
                "offsetAgainstInvoice",
                "Offset against invoice",
                wish="Offsetting has no public endpoint.",
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
            "kind": prop(
                "select",
                "Kind",
                **RO,
                section="general",
                options=[
                    {"value": "correction", "label": "Correction"},
                    {"value": "cancellation", "label": "Cancellation"},
                ],
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
            "costCenter": prop("string", "Cost center", section="general", **_CU),
            "references": prop(
                "embedded",
                "References",
                section="references",
                properties={
                    "customerOrderNumber": prop(
                        "string", "Customer order number", **_CU, filterable=True
                    ),
                    "debtorAccountNumber": prop("string", "Debtor account"),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "issued": prop("date", "Issued", **_CU, filterable=True, sortable=True),
                    # Unlike the invoice, the credit-note v3 DTOs still carry no
                    # `deliveryDate` — the write drops this silently, so the schema must
                    # not advertise it as writable (priorities.json keeps the wish).
                    "serviceDate": prop("date", "Service date", **RO, filterable=True),
                },
            ),
            "taxation": prop("select", "Taxation", section="financials", **_CU),
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
                        "invoiceItem": prop(
                            "reference",
                            "Invoice item",
                            reference="SalesInvoice",
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
                        "description": prop("string", "Description", creatable=True),
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
                        "discountPercent": prop("decimal", "Discount %", creatable=True),
                        "taxRate": prop("string", "Tax rate", creatable=True),
                    }
                },
            ),
            "currency": prop("string", "Currency", section="financials", **_CU),
            "totals": prop(
                "embedded",
                "Totals",
                **RO,
                section="financials",
                properties={
                    "currency": prop("string", "Currency", **RO),
                    "net": prop("string", "Net", **RO),
                    "gross": prop("string", "Gross", **RO),
                    "settled": prop("string", "Settled", **RO),
                    "outstanding": prop("string", "Outstanding", **RO),
                },
            ),
            "settlement": prop(
                "embedded",
                "Settlement",
                section="financials",
                properties={
                    "mode": prop(
                        "select",
                        "Mode",
                        options=[
                            {"value": "refund", "label": "Refund"},
                            {"value": "offset", "label": "Offset"},
                        ],
                    ),
                    "status": prop("select", "Status", **RO),
                    "payments": prop(
                        "collection",
                        "Payments",
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
            "note": prop("string", "Note", section="general", **_CU),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "salesInvoice": prop(
                        "reference",
                        "Sales invoice",
                        reference="SalesInvoice",
                        renderProperty="number",
                        **RO,
                    ),
                    "return": prop(
                        "reference", "Return", reference="Return", renderProperty="number", **RO
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

        fin = r.get("financials") or {}
        tot = r.get("totals") or {}
        cur = fin.get("currency") or (tot.get("net") or {}).get("currency") or "EUR"
        gross = (money((tot.get("gross") or {}).get("amount"), cur) or {}).get("amount")
        settled = "paid" if r.get("paymentStatus") == "paid" else "open"
        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            price = (li.get("price") or {}).get("net") or {}
            invli = li.get("invoiceLineItem") or li.get("salesOrderLineItem") or {}
            items.append(
                {
                    "object": "creditNoteItem",
                    "id": str(li.get("id")) if li.get("id") else None,
                    "position": li.get("order"),
                    "invoiceItem": ref("itm_", invli.get("id"), None, None, "salesInvoices")
                    if invli.get("id")
                    else None,
                    "product": ref(
                        "prd_", p.get("id"), p.get("number"), li.get("name"), "products"
                    ),
                    "description": li.get("description"),
                    "quantity": {"value": li.get("quantity"), "unit": li.get("unit") or "piece"},
                    "unitPrice": money(price.get("amount"), price.get("currency") or cur),
                    "discountPercent": li.get("discount"),
                    "taxRate": li.get("taxRate"),
                }
            )
        inv = r.get("invoice")
        return {
            "object": "creditNote",
            "id": (f"cn_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "kind": "cancellation" if r.get("isCancellationInvoice") else "correction",
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
            "costCenter": r.get("costCenter"),
            "references": {
                "customerOrderNumber": r.get("customerOrderNumber"),
                "debtorAccountNumber": r.get("deviatingDebtorAccountNumber"),
            },
            "dates": {"issued": r.get("documentDate"), "serviceDate": r.get("deliveryDate")},
            "taxation": (fin.get("tax") or {}).get("taxation"),
            "billingAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "items": items,
            "currency": cur,
            "totals": {
                "currency": cur,
                "net": (money((tot.get("net") or {}).get("amount"), cur) or {}).get("amount"),
                "gross": gross,
                "settled": gross if settled == "paid" else "0.00",
                "outstanding": "0.00" if settled == "paid" else gross,
            },
            "settlement": {"mode": "refund", "status": settled, "payments": []},
            "note": r.get("internalComment"),
            "documents": {
                "salesInvoice": ref(
                    "si_",
                    inv.get("id") if isinstance(inv, dict) else inv,
                    None,
                    None,
                    "salesInvoices",
                ),
                "return": None,
            },
            "tags": map_tags(r.get("tags")),
            "customFields": r.get("customFields") or {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    _WRITABLE = {
        "customer",
        "project",
        "costCenter",
        "currency",
        "note",
        "taxation",
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
        "kind",
        "totals",
        "settlement",
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
        if "currency" in model:
            v3.setdefault("financials", {})["currency"] = model["currency"]
        if "taxation" in model:
            v3.setdefault("financials", {}).setdefault("tax", {})["taxation"] = model["taxation"]
        if "note" in model:
            v3["internalComment"] = model["note"]
        if "billingAddress" in model:
            v3["documentAddress"] = self._addr_to_v3(model["billingAddress"])
            if (model["billingAddress"] or {}).get("vatId"):
                v3["vatId"] = model["billingAddress"]["vatId"]
        if "dates" in model and (model["dates"] or {}).get("issued"):
            v3["documentDate"] = model["dates"]["issued"]
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
        if i.get("discountPercent") is not None:
            out["discount"] = i["discountPercent"]
        if i.get("taxRate") is not None:
            out["taxRate"] = i["taxRate"]
        price = line_price_net(i)
        if price is not None:
            out["price"] = price
        return out
