"""Xentral V3 facade · salesOrder — the reference document (docs/01-model.md §4.2).

Outward: the new salesOrder model. Inward: reads/writes Xentral v3
``/api/v3/salesOrders`` and maps 1:1 (references → objects, status → new chain,
trafficLights → holds, financials/totals → money strings). Per ADR-014 there is
no overlay: a field is ``creatable/updatable`` only where the upstream can write
it TODAY; everything else is read-only here and tracked as a blue wish in
priorities.json (a write that includes it answers 409 with the field list).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import (
    _TIMEOUT,
    FacadeAdapterBase,
    line_price_net,
    line_purchase_price_net,
    line_qty,
    RO,
    map_purchase_price,
    map_tags,
    money,
    prop,
    purchase_price_prop,
    ref,
    rejected_item_keys,
    status_map,
    tags_prop,
    tags_to_v3,
)

# Line items on an existing order are their own v3 sub-resource (full CRUD). The
# order PATCH cannot touch them, so `update` with `items` reconciles here.
_SO_LINEITEMS = "/api/v3/salesOrders/{id}/lineItems"

# Per-line availability is computed on the single `get` from one v2 product read
# per line (isStockItem + stockCount) — see _hydrate_availability.
_V2_PRODUCT = "/api/v2/products/{id}"

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
# Fulfilment priority is the neutral face of v3 `fastLane` (a boolean upstream).
_PRIORITY_OPTIONS = [{"value": "normal", "label": "Normal"}, {"value": "high", "label": "High"}]
# The only partial-shipping policy the upstream can represent today; the read
# reports it unconditionally, so the write treats it as the no-op value.
_PARTIAL_SHIPPING = "allowed"
# System trafficLights that represent an actual fulfilment BLOCK (→ holds) when
# they sit in a blocking state. The full raw set is passed through as the read-only
# `trafficLights` field (no interpretation); `holds` is the curated, evidence-based
# subset — the reason a dispatch would be rejected (e.g. stock: false → "Check items
# in stock not passed"). Every id here maps to a stable hold `type`.
_HOLD_LIGHTS = {
    "stock": "stock",
    "stockAvailableFifo": "stock",
    "stockAvailableOpenSupply": "stock",
    "creditLimit": "creditLimit",
    "deliveryBlock": "manual",
    "addressValidation": "address",
}
# A system light blocks when its state is falsy in the "not ok" sense — verified
# against mvp: an out-of-stock order shows stock=false / stockAvailable*="no" while
# every passing check is true. (Polarity is per-light; only these blocking values
# are treated as a hold, everything else stays purely informational.)
_BLOCK_STATES = (False, "no", "notAvailable")


def _light_state(v: Any) -> Any:
    """Normalize a traffic-light state to a stable string (booleans → 'true'/'false';
    upstream string states pass through; None stays None)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _num(v: Any) -> Any:
    """Whole floats → int for clean JSON (2.0 → 2); None and others pass through."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


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
        # create-only: the v3 lineItems sub-resource fixes the product on an existing
        # line, so _reconcile_line_items drops it from the PATCH body.
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
        "purchasePrice": purchase_price_prop(updatable=True),
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
        # Per-line stock availability, computed on the single `get` (one product
        # lookup per line — not on list). See _hydrate_availability.
        "availability": prop(
            "embedded",
            "Availability",
            **RO,
            properties={
                "stockManaged": prop("boolean", "Stock managed", **RO),
                "onHand": prop("decimal", "On hand", **RO),
                "deliverable": prop("decimal", "Deliverable now", **RO),
            },
        ),
    }


class SalesOrderAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="SalesOrder",
        label_en="Sales order",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.salesOrder",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/v3/salesOrders"
    include = "lineItems,lineItems.product,project,address,tags,trafficLights"
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
        # Release / freigeben from draft (v3 release) — the document becomes valid
        # and gets its number. Uniform 'release' op across all documents.
        "release": ("PATCH", "release"),
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
        # Hand the order to logistics (Autoversand): v1 dispatch creates a pick run
        # + delivery note and starts shipping. Requires a released order with
        # positions. An optional {printPickList: true} command prints the pick list.
        "dispatch": {
            "method": "POST",
            "path": "/api/v1/salesOrders/{id}/actions/dispatch",
        },
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
            },
            {
                "key": "fulfillment",
                "label": "Fulfillment",
                "commands": [
                    {
                        "key": "dispatch",
                        "label": "Dispatch (hand to logistics)",
                        "destructive": True,
                        "description": (
                            "Hands the order to logistics (Autoversand): creates a pick "
                            "run and delivery note and starts shipping. Requires a "
                            "released/confirmed order with positions. Optional command "
                            "{printPickList: true} prints the pick list."
                        ),
                        "command": {
                            "type": "object",
                            "properties": {
                                "printPickList": {"type": "boolean", "label": "Print pick list"}
                            },
                        },
                    },
                ],
            },
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
                "Split order (empty shell)",
                description="Splits off an EMPTY partial order (raw v1 createPartialSalesOrder); fill it yourself. Prefer splitOrder to move items in one step.",
            ),
            self.action_def(
                "splitOrder",
                "Split order (move items)",
                destructive=True,
                description=(
                    "Move the given items/quantities into a NEW partial order in one "
                    "step: creates the partial (v1 createPartialSalesOrder), adds the "
                    "moved quantities to it, and reduces this order to the remainder "
                    "(a line fully moved is removed). Together the two orders equal the "
                    "original demand."
                ),
                command={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "label": "Items to move into the new partial order",
                            "items": {
                                "type": "object",
                                "required": ["quantity"],
                                "properties": {
                                    "lineItem": {"type": "string", "label": "Source line item id"},
                                    "product": {
                                        "type": "string",
                                        "label": "Product id (when no lineItem given)",
                                    },
                                    "quantity": {"type": "number", "label": "Quantity to move"},
                                },
                            },
                        }
                    },
                },
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
                    "auto": prop("boolean", "Auto dispatch", **_CU),
                    "priority": prop("select", "Priority", options=_PRIORITY_OPTIONS, **_CU),
                    # No upstream slot: the read hardcodes "allowed". Read-only until
                    # v3 exposes a partial-shipping policy (priorities.json wish).
                    "partialShipping": prop("select", "Partial shipping", **RO),
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
            "trafficLights": prop(
                "collection",
                "Traffic lights",
                **RO,
                section="general",
                node={
                    "properties": {
                        "id": prop("string", "Signal", **RO),
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

        # Raw traffic lights (all of them) pass through untouched so an agent can
        # see every fulfilment signal; `holds` is the curated subset that actually
        # blocks a dispatch (evidence-based polarity — see _HOLD_LIGHTS/_BLOCK_STATES).
        lights: list[dict[str, Any]] = []
        holds = []
        seen_holds: set[str] = set()
        for e in r.get("trafficLights") or []:
            if not isinstance(e, dict) or e.get("id") is None:
                continue
            lights.append({"id": str(e["id"]), "state": _light_state(e.get("state"))})
            hid = e["id"]
            if hid in _HOLD_LIGHTS and e.get("state") in _BLOCK_STATES:
                t = _HOLD_LIGHTS[hid]
                if t not in seen_holds:
                    seen_holds.add(t)
                    holds.append({"type": t, "state": _light_state(e.get("state"))})

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
                    "purchasePrice": map_purchase_price(li, cur),
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
                "partialShipping": _PARTIAL_SHIPPING,
            },
            "holds": holds,
            "trafficLights": lights,
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
        "fulfillmentPolicy",
    }
    _IGNORE = {
        "object",
        "id",
        "number",
        "status",
        "totals",
        "documents",
        "holds",
        "trafficLights",
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
        if "fulfillmentPolicy" in model:
            # autoDispatch + fastLane are writable on v3 create AND update — verified
            # live against mvp 2026-07-27 (write, read back, flip, read back again).
            fp = model["fulfillmentPolicy"] or {}
            if "auto" in fp:
                v3["autoDispatch"] = fp["auto"]
            if "priority" in fp:  # neutral select ↔ boolean fastLane upstream
                v3["fastLane"] = fp["priority"] == "high"
            # partialShipping has no upstream slot and the read hardcodes "allowed".
            # Reject only a genuine CHANGE: echoing the value back (a read-modify-write
            # round trip, which is how most clients PATCH) must stay a no-op, not a 409.
            if fp.get("partialShipping", _PARTIAL_SHIPPING) != _PARTIAL_SHIPPING:
                rejected.add("fulfillmentPolicy.partialShipping")
        # create-only upstream
        if "customer" in model:
            if creating:
                v3["address"] = self._ref_id(model["customer"])
            else:
                rejected.add("customer")  # v3 address is create-only
        if "items" in model:
            # item sub-keys the entity does not model would otherwise vanish silently
            rejected |= rejected_item_keys(model["items"], _item_props())
            if creating:
                doc_cur = model.get("currency") or "EUR"
                v3["lineItems"] = [
                    self._item_to_v3(i, doc_cur) for i in model["items"] if isinstance(i, dict)
                ]
            # On UPDATE the items are NOT sent in the order PATCH body — they are
            # reconciled against the v3 lineItems sub-resource in _write (POST new /
            # PATCH changed / DELETE omitted). So neither emit nor reject them here.
        # anything else the merchant tried to set = a blue wish (not writable today)
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        for k in model:
            if k in self._WRITABLE or k in self._IGNORE:
                continue
            rejected.add(k)
        return v3, rejected

    @staticmethod
    def _item_to_v3(i: dict[str, Any], currency: str = "EUR") -> dict[str, Any]:
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
        price = line_price_net(i, currency)
        if price is not None:
            out["price"] = price
        # Upstream rejects an EK whose currency differs from the document's, so the
        # document currency — not a bare "EUR" — is the fallback when the caller
        # sends only an amount.
        purchase = line_purchase_price_net(i, currency)
        if purchase is not None:
            out["purchasePrice"] = purchase
        return out

    # ---- line-item reconcile on update (v3 lineItems sub-resource) --------
    async def _li_call(  # noqa: ANN001
        self, method, url, token, accept_language, client, payload=None
    ) -> tuple[int, Any]:
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001, ANN202
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    async def _reconcile_line_items(  # noqa: ANN001
        self, up_id, desired, base_url, token, accept_language, client
    ) -> dict[str, Any]:
        """Bring the order's line items to ``desired`` via the v3 lineItems
        sub-resource — the order PATCH cannot touch them. Contract (collection
        replace): an item WITH an existing ``id`` is PATCHed, one WITHOUT an id is
        POSTed (added), and an existing item OMITTED from ``desired`` is DELETEd.
        Returns per-op failures (empty = all ok) so a partial reconcile surfaces as
        a warning, never a silent drop."""
        base = base_url.rstrip("/") + _SO_LINEITEMS.format(id=up_id)
        # current line-item ids (skip text lines — they carry no product)
        st, payload = await self._get(
            base_url,
            token,
            handle=f"so_{up_id}",
            query=[],
            accept_language=accept_language,
            client=client,
        )
        current_ids: list[str] = []
        # The document currency governs what an EK may be sent in — read it off the
        # same fetch instead of defaulting the line items to EUR.
        doc_cur = "EUR"
        if st < 400 and isinstance(payload, dict):
            data = payload.get("data") or {}
            doc_cur = (data.get("financials") or {}).get("currency") or doc_cur
            for li in data.get("lineItems") or []:
                if isinstance(li, dict) and li.get("id") is not None and li.get("type") != "text":
                    current_ids.append(str(li["id"]))
        keep = {
            str(i["id"]) for i in desired if isinstance(i, dict) and i.get("id") not in (None, "")
        }
        failures: dict[str, list[Any]] = {}

        # DELETE the omitted lines first.
        for lid in current_ids:
            if lid not in keep:
                status, body = await self._li_call(
                    "DELETE", f"{base}/{lid}", token, accept_language, client
                )
                if status >= 400:
                    failures.setdefault("delete", []).append(
                        {"id": lid, "status": status, "error": body}
                    )
        # PATCH the kept-with-changes, POST the new ones.
        for it in desired:
            if not isinstance(it, dict):
                continue
            lid = it.get("id")
            v3 = self._item_to_v3(it, doc_cur)
            if lid not in (None, "") and str(lid) in current_ids:
                v3.pop("product", None)  # product is fixed on an existing line
                if not v3:
                    continue  # {id} only → keep unchanged, no-op
                status, resp = await self._li_call(
                    "PATCH", f"{base}/{lid}", token, accept_language, client, v3
                )
                if status >= 400:
                    failures.setdefault("update", []).append(
                        {"id": str(lid), "status": status, "error": resp}
                    )
            else:
                status, resp = await self._li_call("POST", base, token, accept_language, client, v3)
                if status >= 400:
                    failures.setdefault("add", []).append(
                        {"product": it.get("product"), "status": status, "error": resp}
                    )
        return failures

    async def _write(  # noqa: ANN001
        self, method, handle, query, body, base_url, token, accept_language, client
    ):
        """On UPDATE, compose line items on the v3 lineItems sub-resource (the order
        PATCH cannot carry them). Order-level fields still go through the normal
        PATCH; when items are the ONLY change the order PATCH is skipped so an empty
        body is never sent. Create is unchanged (items ride the v3 create body)."""
        try:
            model = json.loads(body or b"{}")
        except (ValueError, TypeError):
            model = {}
        items = model.get("items") if isinstance(model, dict) else None
        is_dry = any(k == "dryRun" and v in ("true", "1") for k, v in query)
        compose = method.upper() != "POST" and isinstance(items, list) and not is_dry
        if not compose:
            return await super()._write(
                method, handle, query, body, base_url, token, accept_language, client
            )

        rest = {k: v for k, v in model.items() if k != "items"}
        if rest:
            resp = await super()._write(
                method,
                handle,
                query,
                json.dumps(rest).encode(),
                base_url,
                token,
                accept_language,
                client,
            )
            if resp.status_code >= 400:
                return resp
        up_id = handle.split("_", 1)[1] if handle and "_" in handle else handle
        failures = await self._reconcile_line_items(
            str(up_id), items, base_url, token, accept_language, client
        )
        st, payload = await self._get(
            base_url,
            token,
            handle=handle,
            query=[],
            accept_language=accept_language,
            client=client,
        )
        if st >= 400:
            return self._json(
                st, payload if isinstance(payload, dict) else {"title": "read-back failed"}
            )
        data = self.map_read((payload.get("data") or {}) if isinstance(payload, dict) else {})
        if failures:
            data["_warnings"] = {"items": failures}
        return self._json(200, {"data": data})

    # ---- per-line availability (single-record hydration) ------------------
    async def request(  # noqa: ANN001
        self, *, method, handle, query, body, base_url, token, accept_language=None, client=None
    ):
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
        # Availability is a single-record convenience — hydrating a whole list page
        # would cost one product read per line per row.
        if method.upper() == "GET" and handle:
            return await self._hydrate_availability(resp, base_url, token, accept_language, client)
        return resp

    async def _hydrate_availability(  # noqa: ANN001
        self, resp, base_url, token, accept_language, client
    ):
        """Attach items[].availability (stockManaged / onHand / deliverable) from one
        v2 product read per line. Best-effort: a line whose product can't be read is
        left without an availability block rather than guessing."""
        if getattr(resp, "status_code", None) != 200:
            return resp
        try:
            body = json.loads(resp.content or b"{}")
        except (ValueError, TypeError):
            return resp
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return resp
        for it in items:
            if not isinstance(it, dict):
                continue
            prod = it.get("product") or {}
            pid = prod.get("id") if isinstance(prod, dict) else prod
            if pid is None:
                continue
            up = str(pid).split("_", 1)[1] if "_" in str(pid) else str(pid)
            q = it.get("quantity") or {}
            ordered = q.get("value") if isinstance(q, dict) else q
            av = await self._product_availability(
                up, ordered, base_url, token, accept_language, client
            )
            if av is not None:
                it["availability"] = av
        return self._json(200, body)

    async def _product_availability(  # noqa: ANN001
        self, up_id, ordered, base_url, token, accept_language, client
    ) -> dict[str, Any] | None:
        """One v2 product read → {stockManaged, onHand, deliverable} for a line.
        Non-stock products carry no stock constraint → the full ordered qty is
        deliverable; stock items deliver min(ordered, on-hand)."""
        url = base_url.rstrip("/") + _V2_PRODUCT.replace("{id}", str(up_id))
        st, resp = await self._li_call("GET", url, token, accept_language, client)
        if st >= 400 or not isinstance(resp, dict):
            return None
        d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        try:
            ord_n = float(ordered) if ordered is not None else None
        except (TypeError, ValueError):
            ord_n = None
        if not d.get("isStockItem"):
            return {"stockManaged": False, "onHand": None, "deliverable": _num(ord_n)}
        try:
            on_hand = float(d.get("stockCount") or 0)
        except (TypeError, ValueError):
            on_hand = 0.0
        deliverable = on_hand if ord_n is None else min(ord_n, max(0.0, on_hand))
        return {"stockManaged": True, "onHand": _num(on_hand), "deliverable": _num(deliverable)}

    # ---- composed split: move items into a new partial order -------------
    async def action(  # noqa: ANN001
        self, *, action_key, handle, body, base_url, token, accept_language=None, client=None
    ):
        if action_key == "splitOrder":
            return await self._split_order(handle, body, base_url, token, accept_language, client)
        return await super().action(
            action_key=action_key,
            handle=handle,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )

    async def _create_partial(  # noqa: ANN001
        self, src_up, base_url, token, accept_language, client
    ) -> tuple[str | None, int, dict[str, Any]]:
        """v1 createPartialSalesOrder → the new (empty) partial order's bare id.
        The id comes back in the body or the Location header. Returns
        ``(partial_up, status, error_body)``."""
        url = f"{base_url.rstrip('/')}/api/v1/salesOrders/{src_up}/actions/createPartialSalesOrder"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001, ANN202
            return await c.request("PATCH", url, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        if resp.status_code >= 400:
            try:
                return None, resp.status_code, resp.json()
            except ValueError:
                return None, resp.status_code, {}
        rid = None
        try:
            b = resp.json()
            if isinstance(b, dict):
                rid = (b.get("data") or {}).get("id")
        except ValueError:
            pass
        if not rid:
            loc = resp.headers.get("Location") or resp.headers.get("location")
            if loc:
                rid = loc.rstrip("/").rsplit("/", 1)[-1] or None
        if rid and "_" in str(rid):
            rid = str(rid).split("_", 1)[1]
        return (str(rid) if rid else None), resp.status_code, {}

    async def _split_order(self, handle, body, base_url, token, accept_language, client):  # noqa: ANN001
        """Move ``command.items`` = [{lineItem|product, quantity}] into a new partial
        order and reduce this order to the remainder — one composed step:
        createPartialSalesOrder → add the moved quantities → PATCH/DELETE the source
        lines. A line fully moved is removed; the two orders sum to the original."""
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        command = envelope.get("command") or {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if not ids:
            return self._json(422, {"title": "splitOrder needs a target order id"})
        src_handle = str(ids[0])
        src_up = src_handle.split("_", 1)[1] if "_" in src_handle else src_handle
        moves = command.get("items")
        if not isinstance(moves, list) or not moves:
            return self._json(
                422, {"title": "splitOrder needs command.items = [{lineItem|product, quantity}]"}
            )

        st, payload = await self._get(
            base_url,
            token,
            handle=src_handle,
            query=[],
            accept_language=accept_language,
            client=client,
        )
        if st >= 400:
            return self._json(
                st, payload if isinstance(payload, dict) else {"title": "read failed"}
            )
        src = self.map_read((payload.get("data") or {}) if isinstance(payload, dict) else {})
        lines = {
            str(li["id"]): li
            for li in (src.get("items") or [])
            if isinstance(li, dict) and li.get("id")
        }

        def _find(m: dict[str, Any]) -> dict[str, Any] | None:
            lid = m.get("lineItem")
            if lid is not None:
                key = str(lid).split("_", 1)[1] if "_" in str(lid) else str(lid)
                if key in lines:
                    return lines[key]
            pid = m.get("product")
            if pid is not None:
                pid = str(pid).split("_", 1)[1] if "_" in str(pid) else str(pid)
                for ln in lines.values():
                    lp = (ln.get("product") or {}).get("id")
                    lpb = str(lp).split("_", 1)[1] if lp and "_" in str(lp) else str(lp)
                    if lpb == pid:
                        return ln
            return None

        resolved: list[tuple[dict[str, Any], float, float | None]] = []
        for m in moves:
            if not isinstance(m, dict):
                continue
            try:
                qty = float(m.get("quantity"))
            except (TypeError, ValueError):
                return self._json(422, {"title": f"splitOrder: bad quantity in {m}"})
            if qty <= 0:
                return self._json(422, {"title": f"splitOrder: quantity must be > 0 in {m}"})
            line = _find(m)
            if line is None:
                return self._json(422, {"title": f"splitOrder: no matching line for {m}"})
            try:
                have = float((line.get("quantity") or {}).get("value"))
            except (TypeError, ValueError):
                have = None
            if have is not None and qty > have:
                return self._json(
                    409,
                    {
                        "title": (
                            f"splitOrder: move {qty} exceeds line quantity {have} "
                            f"(line {line['id']})"
                        )
                    },
                )
            resolved.append((line, qty, have))

        partial_up, pst, perr = await self._create_partial(
            src_up, base_url, token, accept_language, client
        )
        if not partial_up:
            return self._json(
                pst if pst >= 400 else 502,
                perr
                if isinstance(perr, dict) and perr
                else {"title": "createPartialSalesOrder failed"},
            )

        warnings: dict[str, list[Any]] = {}
        part_base = base_url.rstrip("/") + _SO_LINEITEMS.format(id=partial_up)
        src_base = base_url.rstrip("/") + _SO_LINEITEMS.format(id=src_up)

        # add the moved quantities to the partial
        for line, qty, _have in resolved:
            item = {
                "product": line.get("product"),
                "quantity": {"value": _num(qty), "unit": (line.get("quantity") or {}).get("unit")},
                "unitPrice": line.get("unitPrice"),
                "taxRate": line.get("taxRate"),
                # carry the manual EK over, else the split silently re-derives it
                # from the price list and the contribution margin shifts
                "purchasePrice": line.get("purchasePrice"),
            }
            status, resp = await self._li_call(
                "POST",
                part_base,
                token,
                accept_language,
                client,
                self._item_to_v3(item, src.get("currency") or "EUR"),
            )
            if status >= 400:
                warnings.setdefault("add", []).append(
                    {"product": line.get("product"), "status": status, "error": resp}
                )

        # reduce the source order to the remainder
        for line, qty, have in resolved:
            lid = str(line["id"])
            if have is None:
                warnings.setdefault("reduce", []).append(
                    {"id": lid, "error": "unknown source quantity; line not reduced"}
                )
                continue
            remaining = have - qty
            if remaining <= 0:
                status, resp = await self._li_call(
                    "DELETE", f"{src_base}/{lid}", token, accept_language, client
                )
            else:
                status, resp = await self._li_call(
                    "PATCH",
                    f"{src_base}/{lid}",
                    token,
                    accept_language,
                    client,
                    {"quantity": _num(remaining)},
                )
            if status >= 400:
                warnings.setdefault("reduce", []).append(
                    {"id": lid, "status": status, "error": resp}
                )

        st, payload = await self._get(
            base_url,
            token,
            handle=f"so_{partial_up}",
            query=[],
            accept_language=accept_language,
            client=client,
        )
        data = self.map_read((payload.get("data") or {}) if isinstance(payload, dict) else {})
        data["_split"] = {"partialId": f"so_{partial_up}", "sourceId": src_handle}
        if warnings:
            data["_warnings"] = warnings
        return self._json(201, {"data": data})
