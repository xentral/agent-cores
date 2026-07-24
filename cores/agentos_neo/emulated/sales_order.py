"""Xentral V3 facade · salesOrder — the reference document (docs/01-model.md §4.2).

Outward: the new salesOrder model. Inward: reads/writes Xentral v3
``/api/v3/salesOrders`` and maps 1:1 (references → objects, status → new chain,
trafficLights → holds, financials/totals → money strings). Per ADR-014 there is
no overlay: a field is ``creatable/updatable`` only where the upstream can write
it TODAY; everything else is read-only here and tracked as a blue wish in
priorities.json (a write that includes it answers 409 with the field list).
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

# Upstream status → new main-status chain (ADR-015; docs/03 mapping table).
_STATUS = {
    "draft": "draft",
    "released": "confirmed",
    "sent": "confirmed",
    "confirmed": "confirmed",
    "completed": "fulfilled",
    "closed": "closed",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("draft", "confirmed", "fulfilled", "closed", "cancelled")
]
# trafficLights that represent an actual fulfilment BLOCK (→ holds); the rest are
# informational signals, not holds. Polarity/`since`/`by` need product sign-off
# (docs/00-decisions Offene Klärung #3) — kept minimal and honest until then.
_HOLD_LIGHTS = {
    "creditLimit": "creditLimit",
    "deliveryBlock": "manual",
    "addressValidation": "address",
}

# Address sub-field flags: writable both on create + update upstream.
_CU = {"creatable": True, "updatable": True}


def _address_props(*, vat_writable: bool = True) -> dict[str, Any]:
    s = lambda label: prop("string", label, **_CU)  # noqa: E731
    return {
        "name": s("Name"),
        "street": s("Street"),
        "zip": s("Zip"),
        "city": s("City"),
        "country": s("Country"),
        "email": s("Email"),
        "phone": s("Phone"),
        # A document carries ONE VAT id (the billing/document address → top-level
        # v3 `vatId`). The v3 deviating ship-to address has no vatId slot, so it is
        # read-only there (writes were silently dropped).
        "vatId": s("VAT id") if vat_writable else prop("string", "VAT id", **RO),
    }


def _item_props() -> dict[str, Any]:
    return {
        "object": prop("string", "Object", **RO),
        "id": prop("string", "Item id", **RO),
        "position": prop("integer", "Position"),
        # writable on create; v3 has no line-item UPDATE path → wish (priorities.json)
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
        "priceSource": prop("string", "Price source", **RO),
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
        "warehouse": prop("reference", "Warehouse", reference="Warehouse", renderProperty="name"),
        "fulfillment": prop(
            "embedded",
            "Fulfillment",
            **RO,
            properties={
                "shipped": prop("integer", "Shipped", **RO),
                "invoiced": prop("integer", "Invoiced", **RO),
                "returned": prop("integer", "Returned", **RO),
            },
        ),
    }


class SalesOrderAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="SalesOrder",
        label_en="Sales order",
        category="documents",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.salesOrder",
        source_apis=("agentos_neo",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/salesOrders"
    include = "lineItems,lineItems.product,project,address,tags,__internal__trafficLights"
    preview_template = "{{number}}"
    query_aliases = {
        "number": "documentNumber",
        "dates.issued": "documentDate",
        "customer": "address.id",
        "project": "project.id",
        "references.customerOrderNumber": "customerOrderNumber",
        "tags": "tags",
        "references.externalNumber": "externalOrderNumber",
        "references.externalId": "externalOrderId",
        "dates.requestedDelivery": "desiredDeliveryDate",
        "channel": "salesChannel.id",
    }
    filter_value_maps = {
        "status": {"confirmed": "released", "fulfilled": "completed", "closed": "completed"}
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "address": {"label": "Addresses"},
        "items": {"label": "Items"},
        "financials": {"label": "Financials"},
        "shipping": {"label": "Shipping"},
        "flow": {"label": "Document flow"},
    }

    action_map = {
        "confirm": ("PATCH", "release"),
        "close": ("PATCH", "complete"),
        "cancel": ("PATCH", "cancel"),
        "sendConfirmation": ("PATCH", "send"),
        "createSalesInvoice": {
            "method": "POST",
            "path": "/api/v3/invoices/actions/createFromSalesOrder",
            "body": {"salesOrder": {"id": "{id}"}},
        },
        "split": {
            "method": "PATCH",
            "path": "/api/v1/salesOrders/{id}/actions/createPartialSalesOrder",
        },
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd("confirm", "Confirm"),
                    self.step_cmd("close", "Close"),
                    self.step_cmd("cancel", "Cancel"),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "createDeliveryNote",
                "Create delivery note",
                wish="v1 dispatch couples delivery-note creation with shipping side-effects; a clean createDeliveryNote(items?) needs a dedicated endpoint.",
            ),
            self.action_def(
                "createSalesInvoice",
                "Create sales invoice",
                description="Creates the invoice from this order (v3 invoices createFromSalesOrder).",
            ),
            self.action_def(
                "createPickingRun",
                "Create picking run",
                wish="Pick list creation has no public endpoint (05 #12).",
            ),
            self.action_def(
                "addHold",
                "Add hold",
                wish="Holds map to trafficLights — readable, but there is no public write API.",
            ),
            self.action_def(
                "releaseHold",
                "Release hold",
                wish="Holds map to trafficLights — readable, but there is no public write API.",
            ),
            self.action_def(
                "allocateStock",
                "Allocate stock",
                wish="Stock allocation runs upstream automatically; no manual trigger is exposed.",
            ),
            self.action_def(
                "split",
                "Split order",
                description="Splits off a partial order (v1 createPartialSalesOrder).",
            ),
            self.action_def("duplicate", "Duplicate", wish="No duplicate endpoint upstream."),
            self.action_def(
                "downloadPdf",
                "Download PDF",
                wish="No public PDF render endpoint; the archived files at /api/v2/{type}/{id}/files are not yet composed.",
            ),
            self.action_def(
                "sendConfirmation",
                "Send confirmation",
                destructive=True,
                description="Sends the order confirmation to the customer (v3 send — mails the document).",
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
            # customer maps to v3 `address` — create-only upstream (update = wish).
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
                    "externalId": prop("string", "External id", filterable=True),
                    "externalNumber": prop("string", "External number", filterable=True),
                    "paymentTransactionId": prop("string", "Payment transaction id"),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "issued": prop("date", "Issued", **_CU, filterable=True, sortable=True),
                    "requestedDelivery": prop("date", "Requested delivery", **_CU, filterable=True),
                    "earliestFulfillment": prop("date", "Earliest fulfillment", **_CU),
                    "reservation": prop("date", "Reservation", **_CU),
                    "confirmedDelivery": prop("date", "Confirmed delivery", **RO),
                },
            ),
            "billingAddress": prop(
                "embedded", "Billing address", section="address", properties=_address_props()
            ),
            "shippingAddress": prop(
                "embedded",
                "Shipping address",
                section="address",
                properties=_address_props(vat_writable=False),
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
                    "taxes": prop(
                        "collection",
                        "Taxes",
                        **RO,
                        node={
                            "properties": {
                                "rate": prop("string", "Rate", **RO),
                                "percent": prop("decimal", "Percent", **RO),
                                "base": prop("string", "Base", **RO),
                                "amount": prop("string", "Amount", **RO),
                            }
                        },
                    ),
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
                    "status": prop("select", "Payment status", **RO, filterable=True),
                },
            ),
            "shipping": prop(
                "embedded",
                "Shipping",
                section="shipping",
                properties={
                    "method": prop(
                        "reference",
                        "Method",
                        reference="ShippingMethod",
                        renderProperty="name",
                        **_CU,
                    ),
                    "status": prop("select", "Shipping status", **RO, filterable=True),
                    "cost": prop(
                        "embedded",
                        "Cost",
                        **RO,
                        properties={
                            "amount": prop("string", "Amount", **RO),
                            "currency": prop("string", "Currency", **RO),
                        },
                    ),
                },
            ),
            "fulfillmentPolicy": prop(
                "embedded",
                "Fulfillment policy",
                section="shipping",
                properties={
                    "auto": prop("boolean", "Auto dispatch"),
                    "priority": prop("select", "Priority"),
                    "partialShipping": prop("select", "Partial shipping"),
                },
            ),
            "holds": prop(
                "collection",
                "Holds",
                **RO,
                section="general",
                node={
                    "properties": {
                        "type": prop("string", "Type", **RO),
                        "state": prop("string", "State", **RO),
                    }
                },
            ),
            "texts": prop(
                "embedded",
                "Texts",
                section="general",
                properties={
                    "intro": prop("string", "Intro", **_CU),
                    "outro": prop("string", "Outro", **_CU),
                },
            ),
            "note": prop("string", "Note", section="general", **_CU),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "quote": prop(
                        "reference", "Quote", reference="Quote", renderProperty="number", **RO
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
                },
            ),
            "tags": tags_prop(writable=True),
            "discounts": prop(
                "collection",
                "Discounts",
                **RO,
                section="financials",
                node={
                    "properties": {
                        "kind": prop("string", "Kind", **RO),
                        "description": prop("string", "Description", **RO),
                        "amount": prop("string", "Amount", **RO),
                    }
                },
            ),
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
        doc_addr = r.get("documentAddress") or {}
        terms = fin.get("paymentTerms") or {}

        holds = []
        for e in r.get("trafficLights") or []:
            if isinstance(e, dict) and e.get("id") in _HOLD_LIGHTS:
                holds.append({"type": _HOLD_LIGHTS[e["id"]], "state": e.get("state")})

        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict):
                continue
            if li.get("type") == "text":
                items.append(
                    {
                        "object": "textItem",
                        "id": str(li.get("id")) if li.get("id") else None,
                        "position": li.get("order"),
                        "text": li.get("name"),
                    }
                )
                continue
            p = li.get("product") or {}
            price = (li.get("price") or {}).get("net") or {}
            items.append(
                {
                    "object": "salesOrderItem",
                    "id": str(li.get("id")) if li.get("id") else None,
                    "position": li.get("order"),
                    "product": ref(
                        "prd_", p.get("id"), p.get("number"), li.get("name"), "products"
                    ),
                    "description": li.get("description"),
                    "quantity": {"value": li.get("quantity"), "unit": li.get("unit") or "piece"},
                    "unitPrice": money(price.get("amount"), price.get("currency") or cur),
                    "priceSource": None,
                    "discountPercent": li.get("discount"),
                    "taxRate": li.get("taxRate"),
                }
            )

        offer = r.get("offer")
        offer_id = offer.get("id") if isinstance(offer, dict) else offer

        return {
            "object": "salesOrder",
            "discounts": None,
            "id": (f"so_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "customer": ref(
                "cus_",
                (r.get("address") or {}).get("id"),
                r.get("customerNumber"),
                doc_addr.get("name"),
                "customers",
            ),
            "channel": ref("ch_", (r.get("salesChannel") or {}).get("id"), None, None, "channels"),
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
                "externalId": r.get("externalOrderId"),
                "externalNumber": r.get("externalOrderNumber"),
                "paymentTransactionId": None,
            },
            "dates": {
                "issued": r.get("documentDate"),
                "requestedDelivery": r.get("desiredDeliveryDate"),
                "earliestFulfillment": r.get("earliestFulfillmentDate"),
                "reservation": r.get("reservationDate"),
                "confirmedDelivery": None,
            },
            "billingAddress": addr(doc_addr, r.get("vatId")),
            "shippingAddress": addr(r.get("deviatingShipToAddress")),
            "items": items,
            "currency": cur,
            "totals": {
                "currency": cur,
                "net": (money((tot.get("net") or {}).get("amount"), cur) or {}).get("amount"),
                "gross": (money((tot.get("gross") or {}).get("amount"), cur) or {}).get("amount"),
                "taxes": [],
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
                "status": None,
            },
            "shipping": {
                "method": ref(
                    "ship_",
                    (r.get("shippingMethod") or {}).get("id"),
                    None,
                    None,
                    "shippingMethods",
                ),
                "status": None,
                "cost": None,
            },
            "fulfillmentPolicy": {
                "auto": r.get("autoDispatch"),
                "priority": "high" if r.get("fastLane") else "normal",
                "partialShipping": "allowed",
            },
            "holds": holds,
            "texts": {"intro": r.get("bodyIntroduction"), "outro": r.get("bodyOutroduction")},
            "note": r.get("internalComment"),
            "documents": {
                "quote": ref("quo_", offer_id, None, None, "quotes"),
                "deliveryNotes": [],
                "salesInvoices": [],
            },
            "tags": map_tags(r.get("tags")),
            "customFields": r.get("customFields") or {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # ---- write: new model → v3 (only what the upstream can set today) -----
    # Top-level model keys the upstream cannot write → 409 blue wishes.
    _WRITABLE = {
        "customer",
        "project",
        "costCenter",
        "currency",
        "note",
        "texts",
        "billingAddress",
        "shippingAddress",
        "items",
        "dates",
        "tags",
        "references",
        "shipping",
    }
    _IGNORE = {
        "object",
        "id",
        "number",
        "status",
        "totals",
        "documents",
        "holds",
        "createdAt",
        "updatedAt",
    }

    @staticmethod
    def _ref_id(v: Any) -> dict[str, Any] | None:
        if isinstance(v, dict):
            ident = v.get("id") or v.get("number")
            if ident is None:
                return None
            return {"id": str(ident).split("_", 1)[1] if "_" in str(ident) else str(ident)}
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
        if "note" in model:
            v3["internalComment"] = model["note"]
        if "texts" in model:
            t = model["texts"] or {}
            if "intro" in t:
                v3["bodyIntroduction"] = t["intro"]
            if "outro" in t:
                v3["bodyOutroduction"] = t["outro"]
        if "billingAddress" in model:
            v3["documentAddress"] = self._addr_to_v3(model["billingAddress"])
            if (model["billingAddress"] or {}).get("vatId"):
                v3["vatId"] = model["billingAddress"]["vatId"]
        if "shippingAddress" in model:
            # deviatingShipToAddress is the wire key on BOTH create and update
            # (verified live: deviatingDeliveryAddress is silently dropped on
            # PATCH). None clears the deviating address (nullable on update).
            v3["deviatingShipToAddress"] = (
                None
                if model["shippingAddress"] is None
                else self._addr_to_v3(model["shippingAddress"])
            )
        if "dates" in model:
            d = model["dates"] or {}
            if d.get("issued"):
                v3["documentDate"] = d["issued"]
            # Fulfillment dates: writable on v3 since API-733 (mvp 26.30.1).
            if "requestedDelivery" in d:
                v3["desiredDeliveryDate"] = d["requestedDelivery"]
            if "earliestFulfillment" in d:
                v3["earliestFulfillmentDate"] = d["earliestFulfillment"]
            if "reservation" in d:
                v3["reservationDate"] = d["reservation"]
        if "references" in model:
            refs = model["references"] or {}
            if "customerOrderNumber" in refs:  # v3 customerOrderNumber (API-731 parity)
                v3["customerOrderNumber"] = refs["customerOrderNumber"]
        if "shipping" in model:
            sh = model["shipping"] or {}
            if "method" in sh:  # v3 shippingMethod {id} (API-729 parity)
                v3["shippingMethod"] = self._ref_id(sh["method"])
        # create-only upstream
        if "customer" in model:
            if creating:
                v3["address"] = self._ref_id(model["customer"])
            else:
                rejected.add("customer")  # v3 address is create-only
        if "items" in model:
            if creating:
                v3["lineItems"] = [
                    self._item_to_v3(i) for i in model["items"] if isinstance(i, dict)
                ]
            else:
                rejected.add("items")  # v3 has no line-item update path
        # anything else the merchant tried to set = a blue wish (not writable today)
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
