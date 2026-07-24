from __future__ import annotations

from copy import deepcopy
from typing import Any

from entity_registry.core_sdk import EmulationManifest
from .business_document import (
    BusinessDocumentAdapterBase,
    _postal_address_properties,
    _property,
)
from .csv_contract import DOCUMENT_COMMON_ROOT, LINE_ITEM_COMMON, field


def _status_options(language: str) -> list[dict[str, str]]:
    statuses = [
        ("draft", "Entwurf", "Draft"),
        ("released", "Freigegeben", "Released"),
        ("sent", "Versendet", "Sent"),
        ("completed", "Abgeschlossen", "Completed"),
        ("cancelled", "Storniert", "Cancelled"),
    ]
    return [{"value": value, "label": en} for value, de, en in statuses]


class CreditNoteAdapter(BusinessDocumentAdapterBase):
    manifest = EmulationManifest(
        key="SalesCreditNote",
        label_en="Sales Credit Note",
        category="Accounting",
        rollout_batch="sales-credit-note-v2",
        adapter="v3-credit-note",
        source_apis=("/api/v3/creditNotes",),
        operations=("list", "read", "create", "update", "delete"),
        # Xentral's native catalog exposes the credit note as ``creditNote``;
        # this emulator replaces it, so hide the native namesake.
        replaces_native_keys=("creditNote",),
    )

    base_path = "/api/v3/creditNotes"
    # Grounded in CreditNotesActionsController (release/send/write-protection/log).
    lifecycle_actions = ("release",)
    preview_property_names = (
        "documentNumber",
        "status",
        "businessPartnerId",
        "customerNumber",
        "documentDate",
        "deliveryDate",
        "totalAmount",
        "currency",
        "paymentStatus",
        "invoiceId",
    )
    root_property_aliases = {
        "address": "businessPartnerId",
        "documentStatus": "status",
        "project": "projectId",
        "sales": "salesId",
        "invoice": "invoiceId",
        "costCenterValue": "costCenter",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "deliveryNoteId": field(
            "reference",
            section="references",
            reference="DeliveryNote",
            renderProperty="documentNumber",
        ),
        "paymentMethodName": field(section="financials"),
        "amountPaid": field("decimal", section="financials", access="readOnly"),
        "targetAmount": field("decimal", section="financials", access="readOnly"),
        "totalAmount": field("decimal", section="financials", access="readOnly"),
        "isNotRevenueReducing": field("boolean", section="financials"),
        "cashDiscountAmount": field("decimal", section="financials"),
        "isCashDiscountCalculated": field("boolean", section="financials", access="readOnly"),
        "shouldShowTax": field("boolean", section="financials"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "creditNoteId": field(
            "reference", reference="SalesCreditNote", renderProperty="documentNumber"
        ),
    }
    legacy_root_field_map = {
        "lieferschein": "deliveryNoteId",
        "zahlungsweise": "paymentMethodName",
        "ist": "amountPaid",
        "soll": "targetAmount",
        "gesamtsumme": "totalAmount",
        "nicht_umsatzmindernd": "isNotRevenueReducing",
        "skontobetrag": "cashDiscountAmount",
        "skontoberechnet": "isCashDiscountCalculated",
        "anzeigesteuer": "shouldShowTax",
        "waehrung": "currency",
        "email": "email",
        "telefon": "phone",
        "telefax": "fax",
    }
    legacy_line_item_field_map = {
        "gutschrift": "creditNoteId",
        "artikel": "productId",
        "projekt": "projectId",
        "waehrung": "currency",
        "status": "status",
        "umsatzsteuer": "salesTaxType",
        "kostenstelle": "costCenter",
        "erloese": "revenueAccountNumber",
        "einkaufspreiswaehrung": "purchasePriceCurrency",
        "einkaufspreisurspruenglich": "originalPurchasePrice",
        "einkaufspreisid": "purchasePriceId",
        "erloesefestschreiben": "isRevenueFixed",
        "ohnepreis": "hasNoPrice",
        "ausblenden_im_pdf": "shouldHideOnPdf",
        "skontobetrag": "cashDiscountAmount",
        "steuerbetrag": "taxAmount",
        "umsatz_netto_einzeln": "netRevenueItemSingle",
        "umsatz_netto_gesamt": "netRevenueItemTotal",
        "umsatz_brutto_einzeln": "grossRevenueItemSingle",
        "umsatz_brutto_gesamt": "grossRevenueItemTotal",
    }
    list_include = "address,lineItems,customFields,lineItems.customFields,lineItems.product,project,tags,activity"
    detail_include = list_include
    payload_field_map = {
        "deliveryDate": "lieferdatum",
        # customerOrderNumber not remapped: v3 (API-731) accepts the English name.
        "deviatingDebtorAccountNumber": "kundennummer_buchhaltung",
        "costCenterValue": "kostenstelle",
        "invoice": "rechnungid",
        "isCancellationInvoice": "stornorechnung",
    }
    payload_object_fields = {
        "address",
        "project",
        "sales",
        "invoice",
        "editor",
    }
    payload_read_only_fields = BusinessDocumentAdapterBase.payload_read_only_fields | {
        "paymentStatus",
        "effectiveAddresses",
    }
    query_aliases = {
        "documentStatus": "status",
        "address": "address.id",
        "documentAddress.name": "documentAddress.name",
        "documentAddress.country": "documentAddress.country",
        "project": "project.id",
        "sales": "sales.id",
        "invoice": "invoice.id",
        "costCenterValue": "costCenter",
        "paymentStatus": "paymentStatus",
        "isCancellationInvoice": "stornorechnung",
    }
    # No filter key on /creditNotes for these (verified against the live
    # allow-list), so don't advertise them as filterable.
    filterable_removals = ("customerNumber", "masterReferenceNumber")

    def _extra_root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "paymentStatus": p(
                "select",
                "Payment status",
                access="readOnly",
                filterable=True,
                section="financials",
                options=[
                    {"value": "pending", "label": "Pending"},
                    {"value": "partiallyPaid", "label": "Partially paid"},
                    {"value": "paid", "label": "Paid"},
                ],
            ),
            "debtStatus": p(
                "embedded",
                "Debt status",
                section="financials",
                properties={
                    "doneAt": p("date", "Done at"),
                    "comment": p("string", "Comment"),
                },
            ),
            "isCancellationInvoice": p(
                "boolean",
                "Cancellation invoice",
                section="financials",
            ),
            "invoice": p(
                "reference",
                "Invoice",
                reference="SalesInvoice",
                renderProperty="documentNumber",
                section="references",
            ),
            "effectiveAddresses": p(
                "embedded",
                "Effective addresses",
                access="readOnly",
                section="address",
                properties={
                    "billTo": p("embedded", "Bill to", properties=_postal_address_properties()),
                },
            ),
            "deliveryDate": p("date", "Lieferdatum", "Delivery date", section="references"),
            "customerOrderNumber": p(
                "string", "Kundenbestellnummer", "Customer order number", section="references"
            ),
            "deviatingDebtorAccountNumber": p(
                "string",
                "Deviating debtor account number",
                section="references",
            ),
        }

    def _record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        def ref(value: Any) -> dict[str, Any] | None:
            if value in (None, ""):
                return None
            if isinstance(value, dict):
                if value.get("id") is not None:
                    value["id"] = str(value["id"])
                return value
            return {"id": str(value)}

        record["documentStatus"] = record.pop("status", None)
        record["isWriteProtected"] = record.pop("writeProtection", False)
        record["address"] = ref(record.pop("address", None))
        record["documentAddress"] = self._postal_address_from_v3(
            record.pop("documentAddress", None), record.pop("vatId", None)
        )
        record["project"] = ref(record.pop("project", None))
        record["sales"] = ref(record.pop("vertriebid", None))
        record["invoice"] = ref(record.pop("rechnungid", None))
        record["customerOrderNumber"] = record.pop(
            "ihrebestellnummer", record.get("customerOrderNumber")
        )
        record["deviatingDebtorAccountNumber"] = record.pop("kundennummer_buchhaltung", None)
        record["deliveryDate"] = record.pop("lieferdatum", None)
        record["isCancellationInvoice"] = record.pop("stornorechnung", None)
        record["paymentStatus"] = record.pop("zahlungsstatus", None)
        record["debtStatus"] = {
            "doneAt": record.pop("manuell_vorabbezahlt", None),
            "comment": record.pop("manuell_vorabbezahlt_hinweis", None),
        }
        record["costCenterValue"] = record.pop("kostenstelle", None)
        if isinstance(record.get("lineItems"), list):
            record["lineItems"] = [
                self._line_item_transform(item) if isinstance(item, dict) else item
                for item in record["lineItems"]
            ]
        if record.get("documentAddress") is not None:
            record["effectiveAddresses"] = {"billTo": deepcopy(record.get("documentAddress"))}
        return record

    def _payload_nested(self, key: str, value: Any) -> dict[str, Any] | None:
        if key == "documentAddress":
            address, vat_id = self._postal_address_to_v3(value)
            return {"documentAddress": address, **({"vatId": vat_id} if vat_id is not None else {})}
        if key == "debtStatus" and isinstance(value, dict):
            out: dict[str, Any] = {}
            if "doneAt" in value:
                out["manuell_vorabbezahlt"] = value.get("doneAt")
            if "comment" in value:
                out["manuell_vorabbezahlt_hinweis"] = value.get("comment")
            return out
        if key == "invoice":
            if value in (None, ""):
                return {"rechnungid": None}
            if isinstance(value, dict):
                return None
            return {"rechnungid": {"id": str(value)}}
        if key in {"address", "project", "sales", "editor"}:
            if value in (None, ""):
                return {self.payload_field_map.get(key, key): None}
            if isinstance(value, dict):
                return None
            return {self.payload_field_map.get(key, key): {"id": str(value)}}
        return None
