"""Xentral V3 facade · salesInvoice — Ausgangsrechnung (docs/01-model.md §4.4).

Reads Xentral v3 ``/api/v3/invoices`` and maps into the new model: references →
objects, financials/totals → money strings, dunning + paymentStatus surfaced,
salesOrder/deliveryNote back-links into ``documents``. Per ADR-014, only fields
the upstream can write today are ``creatable/updatable``; the rest (payment terms,
dunning, eInvoice, debtor account, service date, custom fields) are blue wishes.
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
    "open": "open",
    "released": "open",
    "sent": "open",
    "paid": "paid",
    "completed": "paid",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("draft", "open", "paid", "cancelled")
]
_PAY_OPTIONS = [{"value": v, "label": v} for v in ("unpaid", "partiallyPaid", "paid")]
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
        "totals": prop(
            "embedded",
            "Item totals",
            **RO,
            properties={
                "net": prop("string", "Net", **RO),
                "tax": prop("string", "Tax", **RO),
                "gross": prop("string", "Gross", **RO),
            },
        ),
    }


class SalesInvoiceAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="SalesInvoice",
        label_en="Sales invoice",
        category="documents",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.salesInvoice",
        source_apis=("agentos_neo",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/invoices"
    include = "lineItems,lineItems.product,project,address,tags"
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
        "payment.status": "paymentStatus",
    }
    filter_value_maps = {
        "status": {"open": "released", "paid": "completed"},
        "payment.status": {"unpaid": "pending"},
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "address": {"label": "Address"},
        "items": {"label": "Items"},
        "financials": {"label": "Financials"},
        "dunning": {"label": "Dunning"},
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
                description="Send the invoice to the customer (v3 send — mails the document).",
            ),
            self.action_def(
                "registerPayment",
                "Register payment",
                wish="The payments API is not public — no endpoint to register an incoming payment.",
            ),
            self.action_def("remind", "Send reminder", wish="Dunning has no public API."),
            self.action_def(
                "createCreditNote",
                "Create credit note",
                wish="No createFrom endpoint for credit notes upstream.",
            ),
            self.action_def("writeOff", "Write off", wish="Write-offs have no public endpoint."),
            self.action_def(
                "downloadPdf",
                "Download PDF",
                wish="No public PDF render endpoint; the archived files at /api/v2/{type}/{id}/files are not yet composed.",
            ),
            self.action_def(
                "downloadEInvoice",
                "Download e-invoice",
                wish="E-invoice (XRechnung/ZUGFeRD) rendering is not exposed via the public API.",
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
            "fixedAt": prop("datetime", "Fixed at", **RO, section="general"),
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
                    ),
                    "debtorAccountNumber": prop("string", "Debtor account", filterable=True),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "issued": prop("date", "Issued", **_CU, filterable=True, sortable=True),
                    "serviceDate": prop("date", "Service date"),
                    "servicePeriod": prop("string", "Service period", **RO),
                },
            ),
            "taxation": prop("select", "Taxation", section="financials", **_CU),
            "billingAddress": prop(
                "embedded", "Billing address", section="address", properties=_address_props()
            ),
            "items": prop(
                "collection", "Items", section="items", node={"properties": _item_props()}
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
                    "paid": prop("string", "Paid", **RO),
                    "outstanding": prop("string", "Outstanding", **RO),
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
                    "dueDate": prop("date", "Due date", **RO),
                    "discountDate": prop("date", "Discount date", **RO),
                    "status": prop(
                        "select", "Payment status", **RO, options=_PAY_OPTIONS, filterable=True
                    ),
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
            "dunning": prop(
                "embedded",
                "Dunning",
                section="dunning",
                properties={
                    "level": prop("integer", "Level", **RO),
                    "blocked": prop("boolean", "Blocked"),
                    "lastReminderAt": prop("datetime", "Last reminder", **RO),
                    "note": prop("string", "Note"),
                },
            ),
            "eInvoice": prop(
                "embedded",
                "E-invoice",
                **RO,
                section="financials",
                properties={
                    "format": prop("string", "Format", **RO),
                    "status": prop("string", "Status", **RO),
                    "buyerReference": prop("string", "Buyer reference", **RO),
                    "downloadUrl": prop("string", "Download URL", **RO),
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
                    "deliveryNotes": prop(
                        "collection",
                        "Delivery notes",
                        **RO,
                        node={
                            "properties": {
                                "id": prop("string", "ID", **RO),
                                "number": prop("string", "Number", **RO),
                            }
                        },
                    ),
                    "creditNotes": prop(
                        "collection",
                        "Credit notes",
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

        fin = r.get("financials") or {}
        tot = r.get("totals") or {}
        cur = fin.get("currency") or (tot.get("net") or {}).get("currency") or "EUR"
        terms = fin.get("paymentTerms") or {}
        dun = r.get("dunningSettings") or {}
        gross = (money((tot.get("gross") or {}).get("amount"), cur) or {}).get("amount")
        pay_status = {"paid": "paid", "partiallyPaid": "partiallyPaid"}.get(
            r.get("paymentStatus"), "unpaid"
        )
        paid = gross if pay_status == "paid" else None
        outstanding = "0.00" if pay_status == "paid" else gross

        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            price = (li.get("price") or {}).get("net") or {}
            soli = li.get("salesOrderLineItem") or {}
            items.append(
                {
                    "object": "salesInvoiceItem",
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
                    "unitPrice": money(price.get("amount"), price.get("currency") or cur),
                    "discountPercent": li.get("discount"),
                    "taxRate": li.get("taxRate"),
                }
            )

        so = r.get("salesOrder")
        so_id = so.get("id") if isinstance(so, dict) else so
        dn = r.get("deliveryNote")
        dn_ref = ref(
            "dn_", dn.get("id") if isinstance(dn, dict) else dn, None, None, "deliveryNotes"
        )

        return {
            "object": "salesInvoice",
            "id": (f"si_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "fixedAt": None,
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
            "references": {
                "customerOrderNumber": r.get("customerOrderNumber"),
                "debtorAccountNumber": r.get("deviatingDebtorAccountNumber"),
            },
            "dates": {
                "issued": r.get("documentDate"),
                "serviceDate": r.get("deliveryDate"),
                "servicePeriod": None,
            },
            "taxation": (fin.get("tax") or {}).get("taxation"),
            "billingAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "items": items,
            "currency": cur,
            "totals": {
                "currency": cur,
                "net": (money((tot.get("net") or {}).get("amount"), cur) or {}).get("amount"),
                "gross": gross,
                "paid": paid,
                "outstanding": outstanding,
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
                "dueDate": None,
                "discountDate": None,
                "status": pay_status,
                "payments": [],
            },
            "dunning": {
                "level": dun.get("level"),
                "blocked": bool(dun.get("blocked")),
                "lastReminderAt": None,
                "note": dun.get("comment"),
            },
            "eInvoice": {
                "format": "none",
                "status": None,
                "buyerReference": None,
                "downloadUrl": None,
            },
            "note": r.get("internalComment"),
            "documents": {
                "salesOrder": ref("so_", so_id, None, None, "salesOrders"),
                "deliveryNotes": [dn_ref] if dn_ref else [],
                "creditNotes": [],
            },
            "tags": map_tags(r.get("tags")),
            "customFields": r.get("customFields") or {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # ---- write: new model → v3 -------------------------------------------
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
        "fixedAt",
        "totals",
        "documents",
        "holds",
        "eInvoice",
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
        if "dates" in model:
            d = model["dates"] or {}
            if d.get("issued"):
                v3["documentDate"] = d["issued"]
            if "serviceDate" in d:
                rejected.add("dates.serviceDate")
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
            if "customerOrderNumber" in refs:  # v3 customerOrderNumber
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
        price = line_price_net(i)
        if price is not None:
            out["price"] = price
        return out
