"""Xentral V3 facade · quote — Angebot (docs/01-model.md §4.1).

Reads Xentral v3 ``/api/v3/offers`` and maps into the new quote model. Structurally
close to salesOrder. Per ADR-014 only upstream-writable fields are creatable/
updatable; the rest are blue wishes.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import (
    REQUIRED,
    FacadeAdapterBase,
    line_price_net,
    contribution_margin_prop,
    item_totals_prop,
    line_purchase_price_net,
    line_qty,
    RO,
    map_item_totals,
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

_STATUS = {
    "draft": "draft",
    "released": "sent",
    "sent": "sent",
    # OfferStatus upstream also has `commissioned` (beauftragt) and `ordered`
    # (bestellt); both were missing. `commissioned` is the operational accepted
    # state — the tenant audit counts 605,663 of them against ZERO `angenommen`,
    # which no code path writes. mvp happens to carry none, so the verify run
    # could not see it (docs/08-quote-actions-befund.md).
    "commissioned": "accepted",
    "ordered": "accepted",
    "accepted": "accepted",
    "completed": "accepted",
    "declined": "declined",
    "expired": "expired",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("draft", "sent", "accepted", "declined", "expired", "cancelled")
]
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


class QuoteAdapter(FacadeAdapterBase):
    native_search_fields = (
        "number",
        "dates.issued",
        "billingAddress.name",
        "billingAddress.email",
        "billingAddress.zip",
        "references.customerInquiryNumber",
    )
    manifest = EmulationManifest(
        key="Quote",
        label_en="Quote",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.quote",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/v3/offers"
    renders_pdf = True
    reconciles_line_items = True
    include = "lineItems,lineItems.product,project,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
        "items.product": "lineItems.product.id",
        "number": "documentNumber",
        "dates.issued": "documentDate",
        "customer": "address.id",
        "project": "project.id",
        "references.customerInquiryNumber": "customerReference",
        "dates.validUntil": "validUntilDate",
        "dates.requestedDelivery": "desiredDeliveryDate",
        "dates.expectedOrderDate": "plannedOrderDate",
        "tags": "tags",
    }
    filter_value_maps = {
        "status": {
            "sent": "released",
            "accepted": "completed",
            "declined": "cancelled",
            "expired": "completed",
        }
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
        # Release / freigeben from draft — the document leaves 'draft', becomes
        # valid and gets its number from the number range. v3 action is 'release'
        # (uniform across all documents; matches the Xentral UI 'Freigeben').
        "release": ("PATCH", "release"),
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
                    self.step_cmd(
                        "accept",
                        "Accept",
                        wish=(
                            "No upstream endpoint. v3 offers has exactly release, cancel, "
                            "send, logActivity, setWriteProtection and removeWriteProtection; "
                            "UpdateOfferData carries no status, and offers do not exist in "
                            "v1/v2 at all. The transition happens only in the UI. Worth "
                            "settling first: the status 'accepted' (angenommen) is written by "
                            "no code path and occurs in no tenant — the state the ERP really "
                            "keeps is 'commissioned' (beauftragt), reached as a side effect of "
                            "converting the quote into a sales order."
                        ),
                    ),
                    self.step_cmd(
                        "decline",
                        "Decline",
                        wish=(
                            "No upstream endpoint. Declining sets angebot.status='abgelehnt' "
                            "and closes open follow-ups — two SQL updates inside the legacy "
                            "page controller, with no service class, no API route and no "
                            "event. The legacy XML API cannot reach it either: ApiBelegEdit "
                            "whitelists the status values and 'abgelehnt' is not among them."
                        ),
                    ),
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
                description="Send the quote to the customer (v3 send — mails the document).",
            ),
            self.action_def(
                "convertToSalesOrder",
                "Convert to sales order",
                wish=(
                    "No conversion endpoint we can reach. v3 has createFromSalesOrder and "
                    "createFromDeliveryNote for invoices and returns, but no createFromOffer "
                    "for sales orders, and POST /v3/salesOrders cannot reference a quote "
                    "(the relation is read-only). v1 salesOrders/import is a raw import, not "
                    "a conversion. The logic exists — Erpapi::WeiterfuehrenAngebotZuAuftrag "
                    "copies the document and sets the quote to commissioned — and is exposed "
                    "once, as the legacy XML API /api/v1/AngebotZuAuftrag. That one is "
                    "digest-authenticated, hangs on a legacy permission and already sits "
                    "behind a killswitch, so it is not usable from a bearer-token core."
                ),
            ),
            self.action_def(
                "duplicate",
                "Duplicate",
                wish=(
                    "No duplicate endpoint in any API generation — no v3 action, nothing in "
                    "v1/v2, nothing in the legacy XML API. erpAPI::CopyAngebot() does the "
                    "work behind the UI only. The same function exists for orders, invoices "
                    "and delivery notes, so one uniform actions/duplicate would serve every "
                    "document type at once."
                ),
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
            "customer": prop(
                "reference",
                "Customer",
                **REQUIRED,
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
                description=(
                    "Not filterable — the upstream list endpoint rejects a channel "
                    "filter on this document (verified on mvp)."
                ),
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
                    "customerInquiryNumber": prop(
                        "string", "Customer inquiry number", filterable=True
                    )
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "issued": prop("date", "Issued", **_CU, filterable=True, sortable=True),
                    "validUntil": prop("date", "Valid until", filterable=True),
                    "expectedOrderDate": prop("date", "Expected order date", filterable=True),
                    "requestedDelivery": prop("date", "Requested delivery", filterable=True),
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
                        "description": prop(
                            "string", "Description", creatable=True, updatable=True
                        ),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            **REQUIRED,
                            creatable=True,
                            updatable=True,
                            properties={
                                "value": prop("decimal", "Value"),
                                "unit": prop("string", "Unit"),
                            },
                        ),
                        "unitPrice": prop(
                            "embedded",
                            "Unit price",
                            creatable=True,
                            updatable=True,
                            properties={
                                "amount": prop("decimal", "Amount"),
                                "currency": prop("string", "Currency"),
                            },
                        ),
                        "priceSource": prop("string", "Price source", **RO),
                        "discountPercent": prop(
                            "decimal", "Discount %", creatable=True, updatable=True
                        ),
                        "purchasePrice": purchase_price_prop(updatable=True),
                        "contributionMargin": contribution_margin_prop(),
                        "totals": item_totals_prop(),
                        # Derived upstream from the product's tax class and the customer's tax rule.
                        # v3 accepts taxRate on a line, answers 2xx and keeps its own value — measured
                        # on offers, salesOrders, invoices and purchaseOrders (2026-08-01): sent
                        # "reduced", read back "standard", both on create and on update. Declaring it
                        # writable promised an edit that silently did nothing.
                        "taxRate": prop("string", "Tax rate", **RO),
                        "isOptional": prop("boolean", "Optional"),
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
                    )
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
                    "salesOrders": prop(
                        "collection",
                        "Sales orders",
                        **RO,
                        node={
                            "properties": {
                                "id": prop("string", "ID", **RO),
                                "number": prop("string", "Number", **RO),
                            }
                        },
                    )
                },
            ),
            "tags": tags_prop(writable=True),
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
        cur = fin.get("currency") or (tot.get("net") or {}).get("currency") or "EUR"
        terms = fin.get("paymentTerms") or {}
        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            price = (li.get("price") or {}).get("net") or {}
            items.append(
                {
                    "object": "quoteItem",
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
                    "purchasePrice": map_purchase_price(li, cur),
                    "totals": map_item_totals(li, cur),
                    "contributionMargin": li.get("contributionMargin"),
                    "taxRate": li.get("taxRate"),
                    "isOptional": False,
                }
            )
        return {
            "object": "quote",
            "id": (f"quo_{r.get('id')}" if r.get("id") is not None else None),
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
            "references": {"customerInquiryNumber": r.get("customerReference")},
            "dates": {
                "issued": r.get("documentDate"),
                "validUntil": r.get("validUntilDate"),
                "expectedOrderDate": r.get("plannedOrderDate"),
                "requestedDelivery": r.get("desiredDeliveryDate"),
            },
            "billingAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "shippingAddress": addr(r.get("deviatingShipToAddress")),
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
            "shipping": {
                "method": ref(
                    "ship_",
                    (r.get("shippingMethod") or {}).get("id"),
                    None,
                    None,
                    "shippingMethods",
                )
            },
            "texts": {"intro": r.get("bodyIntroduction"), "outro": r.get("bodyOutroduction")},
            "note": r.get("internalComment"),
            "documents": {"salesOrders": []},
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
        "texts",
        "billingAddress",
        "shippingAddress",
        "items",
        "dates",
        "tags",
        "shipping",
    }
    # `number` is deliberately NOT ignored: a document number always comes from the
    # configured number range, so a caller supplying one must be told it was refused
    # rather than get a 201 and a different number. Upstream would accept it on three
    # of these types (salesOrder / invoice / creditNote, verified on mvp) — declining
    # it everywhere is a product decision, recorded as such in priorities.json.
    _IGNORE = {"object", "id", "status", "totals", "documents", "createdAt", "updatedAt"}

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
            v3["deviatingShipToAddress"] = self._addr_to_v3(model["shippingAddress"])
        if "dates" in model and (model["dates"] or {}).get("issued"):
            v3["documentDate"] = model["dates"]["issued"]
        if "customer" in model:
            if creating:
                v3["address"] = self._ref_id(model["customer"])
            else:
                rejected.add("customer")
        if "items" in model:
            # item sub-keys the entity does not model would otherwise vanish silently
            rejected |= rejected_item_keys(
                model["items"], self.fields()["items"]["node"]["properties"]
            )
            if creating:
                doc_cur = model.get("currency") or "EUR"
                v3["lineItems"] = [
                    self._item_to_v3(i, doc_cur) for i in model["items"] if isinstance(i, dict)
                ]
            # On UPDATE the items are NOT sent in the document PATCH body — they are
            # reconciled against the v3 lineItems sub-resource (POST new / PATCH
            # changed / DELETE omitted). So neither emit nor reject them here.
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        if "shipping" in model:
            sh = model["shipping"] or {}
            if "method" in sh:  # v3 shippingMethod {id} (API-729 parity)
                v3["shippingMethod"] = self._ref_id(sh["method"])
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
