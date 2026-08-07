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

``lineItems`` ride along on the list response, and they are WRITABLE — upstream
manages them as a diff inside the document body, where every entry carries an
``actionIndicator`` of ``Create`` / ``Update`` / ``Delete`` and addresses an
existing row by ``uuid``. That field is required and appears in NO schema:
``GET /api/metadata/supplierInvoice`` does not list it, so it can only be learned
by being rejected for it. Outward the collection keeps the contract the other
documents use — item with an id updates, without an id adds, omitted deletes —
and this adapter translates that into the diff.

Two traps this write path guards against, both measured on mvp 2026-08-02:

* an EMPTY ``POST`` is accepted and books an invoice with no creditor, no date and
  no position. There are no required fields upstream (``requiredOnCreate`` reports
  only ``id``). This core therefore requires ``supplier`` itself.
* ``costCenterValue``, ``documentNumber`` and ``exchangeRate`` answer 2xx and do
  NOT persist. They are refused or unmapped rather than silently dropped.

Note that the entity API publishes no per-field ``creatable``/``updatable`` flags
for ANY entity — absence there is not evidence that a field is read-only. Every
flag in this schema was earned by writing the field, reading it back and
restoring it.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import FacadeAdapterBase, REQUIRED, RO, map_tags, money, prop, ref, tags_prop

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
        # A purchase-invoice line is very often FREE TEXT with no product behind it
        # (a service, a fee). `product` collapses to null there, so the line's name
        # has to live on the line — otherwise the only text a caller can set is
        # unreadable back.
        "name": li.get("productName") or None,
        "description": li.get("description") or None,
        "quantity": {"value": li.get("quantity"), "unit": unit or li.get("packageUnit") or None},
        "unitPrice": {"amount": li.get("netPrice"), "currency": li.get("currency") or doc_currency},
        "taxRate": li.get("taxRate"),
    }


class PurchaseInvoiceAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="PurchaseInvoice",
        label_en="Purchase invoice",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.purchaseInvoice",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
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
                **REQUIRED,
                **_CU,
                reference="Supplier",
                renderProperty="name",
                section="general",
                previewable=True,
                description=(
                    "The creditor. REQUIRED on create by this core, not by upstream: "
                    "an empty POST is accepted and produces an invoice with no "
                    "creditor, no dates and no positions."
                ),
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
                **_CU,
                section="items",
                description=(
                    "Collection replace, the same contract the other documents use: "
                    "an item WITH an id is updated, one WITHOUT an id is added, and "
                    "an existing item OMITTED from the list is deleted."
                ),
                node={
                    "properties": {
                        "object": prop("string", "Object", **RO),
                        "id": prop("string", "Item id", **RO),
                        "product": prop(
                            "reference",
                            "Product",
                            **_CU,
                            reference="Product",
                            renderProperty="name",
                            description=(
                                "Linking a product OVERRIDES the name: upstream fills "
                                "productName from the product record."
                            ),
                        ),
                        "name": prop(
                            "string",
                            "Name",
                            **_CU,
                            description=(
                                "The text on the line. Ignored when `product` is set "
                                "— upstream then fills it from the product record."
                            ),
                        ),
                        "description": prop("string", "Description", **_CU),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            properties={
                                "value": prop("decimal", "Value", **_CU),
                                "unit": prop("string", "Unit", **_CU),
                            },
                        ),
                        "unitPrice": prop(
                            "embedded",
                            "Unit price",
                            properties={
                                "amount": prop("decimal", "Amount", **_CU),
                                "currency": prop("string", "Currency", **_CU),
                            },
                        ),
                        "taxRate": prop("decimal", "Tax rate", **_CU),
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

        if "supplier" in model:
            sup = model["supplier"]
            sid = sup.get("id") if isinstance(sup, dict) else sup
            if sid:
                wire["associatedAddress"] = {"id": str(sid).split("_", 1)[-1]}
            else:
                rejected.add("supplier")

        if isinstance(model.get("items"), list):
            lines = [self._line_to_wire(i) for i in model["items"] if isinstance(i, dict)]
            # `__removedItems` is filled by `_write`, not by a caller (part of the
            # PATH the request took, like storage_location's `__warehouse`).
            lines += [
                {self._ITEM_ACTION: "Delete", "uuid": u}
                for u in (model.get("__removedItems") or [])
            ]
            wire["lineItems"] = lines

        # Everything the model carries but this mapping does not reach — named so a
        # caller sees the write was dropped instead of assuming it landed.
        # `costCenter` and `number` answer 2xx and do NOT persist (measured);
        # the rest is derived upstream.
        for path in ("match", "totals", "approval", "status", "number", "costCenter"):
            if path in model:
                rejected.add(path)
        return wire, rejected

    # Upstream manages line items as a DIFF inside the document body: every entry
    # carries an `actionIndicator` of Create/Update/Delete. The field is required
    # and appears in NO schema — `GET /api/metadata/supplierInvoice` does not list
    # it, so it can only be learned by being rejected (or from the monorepo's own
    # integration test). Update and Delete address the row by `uuid`.
    _ITEM_ACTION = "actionIndicator"

    def _line_to_wire(self, item: dict[str, Any]) -> dict[str, Any]:
        """One model item → one `lineItems` entry, Create or Update."""
        uuid = str(item.get("id") or "").removeprefix("pii_")
        wire: dict[str, Any] = {
            self._ITEM_ACTION: "Update" if uuid else "Create",
        }
        if uuid:
            wire["uuid"] = uuid
        prod = item.get("product")
        pid = prod.get("id") if isinstance(prod, dict) else prod
        if pid:
            wire["product"] = {"id": str(pid).removeprefix("prd_")}
        name = item.get("name") or (prod.get("name") if isinstance(prod, dict) else None)
        if name and not pid:
            wire["productName"] = name
        qty = item.get("quantity") or {}
        if qty.get("value") is not None:
            wire["quantity"] = str(qty["value"])
        if qty.get("unit"):
            wire["packageUnit"] = qty["unit"]
        price = item.get("unitPrice") or {}
        if price.get("amount") is not None:
            wire["netPrice"] = str(price["amount"])
        if price.get("currency"):
            wire["currency"] = price["currency"]
        if item.get("description") is not None:
            wire["description"] = item["description"]
        if item.get("taxRate") is not None:
            wire["taxRate"] = str(item["taxRate"])
        # netPrice is the ONE required attribute of a new line (measured: a Create
        # without it is a 400). Default it rather than let the write fail on a line
        # a caller only wanted to name.
        if not uuid:
            wire.setdefault("netPrice", "0")
        return wire

    def _created_handle(self, resp: Any) -> Any:
        """This entity is addressed by ``uuid``, never by ``id``.

        `GET /api/entity/supplierInvoice/{id}` answers "Entity not found with uuid
        1", and `id` is not filterable either, so the id from a create response
        cannot be turned back into a readable handle. The create response carries
        the whole record including its uuid — take it from there.
        """
        rec = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(rec, dict):
            rec = resp if isinstance(resp, dict) else {}
        return rec.get("uuid") or rec.get("id")

    async def _write(  # noqa: ANN001
        self, method, handle, query, body, base_url, token, accept_language, client
    ):
        """Two things the synchronous mapping cannot do on its own."""
        import json as _json

        try:
            model = _json.loads(body or b"{}")
        except (ValueError, TypeError):
            model = {}
        if not isinstance(model, dict):
            model = {}
        model.pop("__removedItems", None)  # never honour it from a caller

        if method.upper() == "POST":
            # Upstream accepts an EMPTY POST and books an invoice with no creditor,
            # no date and no position (measured on mvp 2026-08-02, four times). A
            # facade that passes that through turns a malformed agent call into a
            # real accounting document, so the creditor is required here.
            sup = model.get("supplier")
            if not (sup.get("id") if isinstance(sup, dict) else sup):
                return self._refuse(
                    422,
                    "purchaseInvoice: `supplier` is required to create an invoice",
                    detail=(
                        "Upstream would accept this and create an invoice with no "
                        "creditor. Name the supplier the invoice came from."
                    ),
                )
        elif isinstance(model.get("items"), list):
            model["__removedItems"] = await self._removed_items(
                model["items"], handle, base_url, token, accept_language, client
            )
            body = _json.dumps(model).encode()

        return await super()._write(
            method, handle, query, body, base_url, token, accept_language, client
        )

    async def _removed_items(  # noqa: ANN001
        self, desired, handle, base_url, token, accept_language, client
    ) -> list[str]:
        """Line uuids that exist upstream but are absent from ``desired``.

        `items` is a collection REPLACE — the same contract the sub-resource
        documents use (see ``_reconcile_line_items``). Upstream expresses removal
        as an explicit ``Delete`` entry, so the omitted rows have to be looked up
        before the write. A failed read yields no removals: dropping a line the
        caller never mentioned is the one outcome worse than keeping it.
        """
        keep = {
            str(i.get("id") or "").removeprefix("pii_")
            for i in desired
            if isinstance(i, dict) and i.get("id")
        }
        status, payload = await self._get(
            base_url,
            token,
            handle=handle,
            query=[],
            accept_language=accept_language,
            client=client,
        )
        if status >= 400 or not isinstance(payload, dict):
            return []
        # `_get` answers the RAW upstream record, not the mapped model — so the rows
        # are `lineItems` carrying a bare `uuid`, not `items` with a `pii_` id.
        current = (payload.get("data") or {}).get("lineItems") or []
        return [
            u
            for i in current
            if isinstance(i, dict) and (u := str(i.get("uuid") or "")) and u not in keep
        ]

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
