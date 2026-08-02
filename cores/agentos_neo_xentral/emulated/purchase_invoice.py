"""Xentral V3 facade · purchaseInvoice — Eingangsrechnung (docs/01-model.md §5.3).

SECOND UPSTREAM (docs/02-ist-analyse §2b): reads the BF entity API
``GET /api/entity/supplierInvoice`` (verified live: full CRUD, 47 fields) instead
of the dead-end v1 liabilities. The BF record carries the whole AP workflow the
v1 layer lacked: ``goodsCheckStatus`` + ``invoiceCheckStatus`` (three-way match),
``paymentStatus``/``isPaid``/``amountPaid``, attachments/ocrFiles (original
document). Gap Nr. 6 (docs/05) is therefore covered upstream — what remains is
OUR write-mapping + live write verification (blue wishes now say so).

BF detail reads route on the record ``uuid``, not the numeric id, and NEITHER is
filterable — so a numeric id cannot be resolved back to a uuid at all. The speaking
id therefore carries the uuid (as Tag and GoodsReceipt already did): before that
every detail read answered ``404 Entity not found with uuid 1``, for all 42 records,
while the manifest showed 39 green ``read`` verdicts — the probe grades ``read``
from the LIST and nothing ever exercised the single read.

``lineItems`` ride along on the list response and are mapped now; the collection
answered ``[]`` on every record while upstream carried positions on six of them.
The node still covers five of a line item's 23 attributes — enough to read what was
invoiced, not yet enough for the three-way match.

Writes are NOT wired yet. The upstream supports them (``operations`` says
create/read/update/delete, and a net-zero PATCH on ``internalComment`` was verified
live), so what remains is our write-mapping. Note that the entity API publishes no
per-field ``creatable``/``updatable`` flags for ANY entity — absence there is not
evidence that a field is read-only.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, map_tags, money, prop, ref, tags_prop

_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("received", "matched", "approved", "paid", "rejected")
]
_CU = {"creatable": True, "updatable": True}
_MATCH_OPTIONS = [{"value": v, "label": v.capitalize()} for v in ("pending", "matched", "mismatch")]


def _status(r: dict[str, Any]) -> str:
    if r.get("documentStatus") == "cancelled":
        return "rejected"
    if r.get("isPaid") or r.get("paymentStatus") == "paid":
        return "paid"
    gc, ic = r.get("goodsCheckStatus"), r.get("invoiceCheckStatus")
    if gc == "accepted" and ic == "accepted":
        return "approved"
    if gc == "accepted" or ic == "accepted":
        return "matched"
    return "received"


def _match_status(r: dict[str, Any]) -> str:
    gc, ic = r.get("goodsCheckStatus"), r.get("invoiceCheckStatus")
    if "rejected" in (gc, ic):
        return "mismatch"
    if gc == "accepted" and ic == "accepted":
        return "matched"
    return "pending"


def _item(li: dict[str, Any], doc_currency: str | None) -> dict[str, Any]:
    """One BF ``lineItems`` row → the model's ``items`` shape.

    These ride along on the LIST response, so mapping them costs no extra call —
    they were simply never mapped, and the collection answered ``[]`` on all 42
    records while upstream carried positions on six of them.

    The node covers five of the 23 attributes a BF line item has (description,
    taxRate, deliveryDate, costCenter, project, purchaseOrder, supplierProductNumber
    and more are still unmapped) — enough to read what was invoiced, not yet enough
    for the three-way match. Widening it belongs with the write path.
    """
    prod = li.get("product") if isinstance(li.get("product"), dict) else None
    unit = li.get("unitOfMeasure")
    unit = unit.get("id") if isinstance(unit, dict) else unit
    return {
        "object": "purchaseInvoiceItem",
        # Same reason as the record id: BF addresses rows by uuid.
        "id": (
            f"pii_{li['uuid']}"
            if li.get("uuid")
            else (f"pii_{li.get('id')}" if li.get("id") is not None else None)
        ),
        "product": ref(
            "prd_",
            (prod or {}).get("id"),
            li.get("supplierProductNumber") or None,
            li.get("productName") or None,
            "products",
        ),
        "quantity": {"value": li.get("quantity"), "unit": unit or li.get("packageUnit") or None},
        "unitPrice": {"amount": li.get("netPrice"), "currency": li.get("currency") or doc_currency},
    }


class PurchaseInvoiceAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="PurchaseInvoice",
        label_en="Purchase invoice",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.purchaseInvoice",
        source_apis=("agentos_neo_xentral",),
        operations=(
            "list",
            "read",
            "update",
        ),  # BF has full CRUD; our write-mapping is not built/verified yet
    )
    v3_path = "/api/entity/supplierInvoice"
    include = ""
    preview_template = "{{number}}"
    bf_sort = True
    # BF rejects equals/in on the tags property — only contains works there.
    filter_op_maps = {"tags": {"equals": "contains", "in": "contains"}}
    query_aliases = {
        "number": "documentNumber",
        "status": "documentStatus",
        "payment.status": "paymentStatus",
        "dates.invoiceDate": "dateOfSupplierInvoice",
        "dates.received": "dateOfEntry",
        "references.supplierInvoiceNumber": "associatedExternalInvoiceNumber",
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "items": {"label": "Items"},
        "financials": {"label": "Financials"},
        "match": {"label": "3-way match"},
        "flow": {"label": "Document flow"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd(
                        "approve",
                        "Approve",
                        wish="The BF InvoiceCheck/DocumentStatus process steps are readable, but the write transition is not exposed. Candidate: v1 liabilities/{id}/actions/release — needs the numeric id, which the BF rows do not carry.",
                    ),
                    self.step_cmd(
                        "reject", "Reject", wish="Rejecting has no exposed write transition."
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "rematch",
                "Re-run 3-way match",
                wish="Matching runs upstream automatically; no re-trigger endpoint is exposed.",
            ),
            self.action_def(
                "registerPayment",
                "Register payment",
                wish="The payments API is not public — no endpoint to register an outgoing payment.",
            ),
            self.action_def(
                "schedulePayment", "Schedule payment", wish="Payment runs have no public API."
            ),
            self.action_def(
                "attachFile",
                "Attach file",
                wish="Candidate: v1 liabilities/{id}/documents (upload) — needs the numeric id, which the BF rows do not carry.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "number": prop(
                "string", "Number", **RO, section="general", filterable=True, previewable=True
            ),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=_STATUS_OPTIONS,
                previewable=True,
            ),
            "supplier": prop(
                "reference",
                "Supplier",
                reference="Supplier",
                renderProperty="name",
                section="general",
                previewable=True,
            ),
            "costCenter": prop("string", "Cost center", section="general"),
            "references": prop(
                "embedded",
                "References",
                section="references",
                properties={
                    "supplierInvoiceNumber": prop(
                        "string",
                        "Supplier invoice number",
                        **_CU,
                        filterable=True,
                        searchable=True,
                    ),
                    "externalReference": prop("string", "External reference", **_CU),
                    "creditorAccountNumber": prop("string", "Creditor account"),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "invoiceDate": prop("date", "Invoice date", **_CU, filterable=True),
                    "received": prop("date", "Received", **_CU, filterable=True),
                    "serviceDate": prop("date", "Service date", **_CU),
                },
            ),
            "clarification": prop(
                "embedded",
                "Clarification",
                section="match",
                properties={
                    "needed": prop("boolean", "Needed", **_CU),
                    "reason": prop("string", "Reason", **_CU),
                },
            ),
            "items": prop(
                "collection",
                "Items",
                section="items",
                node={
                    "properties": {
                        "object": prop("string", "Object", **RO),
                        "id": prop("string", "Item id", **RO),
                        "product": prop(
                            "reference", "Product", reference="Product", renderProperty="name"
                        ),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            properties={
                                "value": prop("decimal", "Value"),
                                "unit": prop("string", "Unit"),
                            },
                        ),
                        "unitPrice": prop(
                            "embedded",
                            "Unit price",
                            properties={
                                "amount": prop("decimal", "Amount"),
                                "currency": prop("string", "Currency"),
                            },
                        ),
                    }
                },
            ),
            "currency": prop("string", "Currency", section="financials"),
            "totals": prop(
                "embedded",
                "Totals",
                **RO,
                section="financials",
                properties={
                    "currency": prop("string", "Currency", **RO),
                    "gross": prop("string", "Gross", **RO),
                    "paid": prop("string", "Paid", **RO),
                    "outstanding": prop("string", "Outstanding", **RO),
                },
            ),
            "match": prop(
                "embedded",
                "Match",
                **RO,
                section="match",
                properties={
                    "status": prop("select", "Status", **RO, options=_MATCH_OPTIONS),
                    "goodsCheck": prop("string", "Goods check", **RO),
                    "invoiceCheck": prop("string", "Invoice check", **RO),
                    "purchaseOrder": prop(
                        "reference",
                        "Purchase order",
                        reference="PurchaseOrder",
                        renderProperty="number",
                        **RO,
                    ),
                },
            ),
            "payment": prop(
                "embedded",
                "Payment",
                section="financials",
                properties={
                    "method": prop("string", "Method", **RO),
                    "dueDate": prop("date", "Due date", **RO),
                    "discountUntil": prop("date", "Discount until", **RO),
                    "paidOn": prop("date", "Paid on", **RO),
                    "status": prop("select", "Payment status", **RO),
                },
            ),
            "files": prop(
                "collection",
                "Files",
                **RO,
                section="flow",
                node={
                    "properties": {
                        "id": prop("string", "ID", **RO),
                        "name": prop("string", "Name", **RO),
                    }
                },
            ),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "purchaseOrder": prop(
                        "reference",
                        "Purchase order",
                        reference="PurchaseOrder",
                        renderProperty="number",
                        **RO,
                    )
                },
            ),
            "approval": prop(
                "embedded",
                "Approval",
                **RO,
                section="general",
                properties={
                    "status": prop("string", "Status", **RO),
                    "by": prop("reference", "By", **RO, reference="User"),
                    "at": prop("datetime", "At", **RO),
                },
            ),
            "tags": tags_prop(writable=False),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """Model → the entity API's wire shape.

        Only the leaves a live net-zero round-trip actually persisted are mapped.
        The entity API publishes no ``creatable``/``updatable`` flags for any entity,
        so the schema here cannot be derived from it — every flag was earned by
        writing the field, reading it back and restoring it. Two that answer 200 and
        do NOT persist are deliberately absent: ``documentNumber`` (the number is
        system-assigned) and ``costCenterValue``.
        """
        wire: dict[str, Any] = {}
        rejected: set[str] = set()

        refs = model.get("references") or {}
        if "supplierInvoiceNumber" in refs:
            wire["associatedExternalInvoiceNumber"] = refs["supplierInvoiceNumber"]
        if "externalReference" in refs:
            wire["externalReference"] = refs["externalReference"]
        if "creditorAccountNumber" in refs:
            rejected.add("references.creditorAccountNumber")

        dates = model.get("dates") or {}
        for mine, theirs in (
            ("invoiceDate", "dateOfSupplierInvoice"),
            ("received", "dateOfEntry"),
            ("serviceDate", "serviceProvidedOn"),
        ):
            if mine in dates:
                wire[theirs] = dates[mine]

        clar = model.get("clarification") or {}
        if "needed" in clar:
            wire["isInNeedOfClarification"] = clar["needed"]
        if "reason" in clar:
            wire["clarificationReason"] = clar["reason"]

        pay = model.get("payment") or {}
        if "dueDate" in pay:
            wire["payableUntil"] = pay["dueDate"]
        if "discountUntil" in pay:
            wire["discountPossibleUntil"] = pay["discountUntil"]

        if "currency" in model:
            wire["currency"] = model["currency"]

        # Everything the model carries but this mapping does not reach yet — named
        # so a caller sees its write was dropped instead of assuming it landed.
        for path in ("items", "match", "totals", "approval", "supplier", "status", "number"):
            if path in model:
                rejected.add(path)
        return wire, rejected

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        cur = r.get("currency") or "EUR"
        gross = (money(r.get("grossTotalAmount"), cur) or {}).get("amount")
        paid = (money(r.get("amountPaid"), cur) or {}).get("amount")
        outstanding = None
        try:
            if gross is not None and paid is not None:
                outstanding = (money(float(gross) - float(paid), cur) or {}).get("amount")
        except (TypeError, ValueError):
            outstanding = None
        assoc = r.get("associatedAddress")
        po = r.get("purchaseOrder")
        po_ref = ref(
            "po_", po.get("id") if isinstance(po, dict) else po, None, None, "purchaseOrders"
        )
        files = [
            {"id": str(f.get("id")), "name": f.get("name") or f.get("filename")}
            for f in (r.get("attachments") or []) + (r.get("ocrFiles") or [])
            if isinstance(f, dict)
        ]
        return {
            "object": "purchaseInvoice",
            # BF entities are fetched by uuid (GET /{id} 404s, GET /{uuid} 200);
            # encode the uuid so the speaking id round-trips through `get` (F3).
            # Neither `id` nor `uuid` is filterable on this entity, so a numeric id
            # cannot be resolved back to a uuid at all — every detail read answered
            # "Entity not found with uuid 1". Tag and GoodsReceipt already do this.
            "id": (
                f"pi_{r['uuid']}"
                if r.get("uuid")
                else (f"pi_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "number": r.get("documentNumber") or None,
            "status": _status(r),
            "supplier": ref(
                "sup_",
                assoc.get("id") if isinstance(assoc, dict) else assoc,
                None,
                (r.get("postalAddress") or {}).get("name") or None,
                "suppliers",
            ),
            "costCenter": r.get("costCenterValue") or None,
            "references": {
                "supplierInvoiceNumber": r.get("associatedExternalInvoiceNumber") or None,
                "externalReference": r.get("externalReference") or None,
                "creditorAccountNumber": None,
            },
            "dates": {
                "invoiceDate": r.get("dateOfSupplierInvoice"),
                "received": r.get("dateOfEntry"),
                "serviceDate": r.get("serviceProvidedOn"),
            },
            "clarification": {
                "needed": r.get("isInNeedOfClarification"),
                "reason": r.get("clarificationReason") or None,
            },
            "items": [_item(li, cur) for li in (r.get("lineItems") or []) if isinstance(li, dict)],
            "currency": cur,
            "totals": {"currency": cur, "gross": gross, "paid": paid, "outstanding": outstanding},
            "match": {
                "status": _match_status(r),
                "goodsCheck": r.get("goodsCheckStatus"),
                "invoiceCheck": r.get("invoiceCheckStatus"),
                "purchaseOrder": po_ref,
            },
            "payment": {
                "method": r.get("paymentMethod"),
                "dueDate": r.get("payableUntil"),
                "discountUntil": r.get("discountPossibleUntil"),
                "paidOn": r.get("paidOn"),
                "status": r.get("paymentStatus"),
            },
            "files": files,
            "tags": map_tags(r.get("tags")),
            "approval": {
                "status": r.get("invoiceCheckStatus"),
                "by": None,
                "at": None,
            },
            "documents": {"purchaseOrder": po_ref},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }
