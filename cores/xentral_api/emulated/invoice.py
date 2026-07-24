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


def _dunning_options(language: str) -> list[dict[str, str]]:
    levels = [
        ("reminder1", "1. Mahnung", "Reminder 1"),
        ("reminder2", "2. Mahnung", "Reminder 2"),
        ("reminder3", "3. Mahnung", "Reminder 3"),
        ("collection", "Inkasso", "Collection"),
    ]
    return [{"value": value, "label": en} for value, de, en in levels]


class InvoiceAdapter(BusinessDocumentAdapterBase):
    manifest = EmulationManifest(
        key="SalesInvoice",
        label_en="Sales Invoice",
        category="Accounting",
        rollout_batch="sales-invoice-v2",
        adapter="v3-invoice",
        source_apis=("/api/v3/invoices",),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v3/invoices"
    # Grounded in InvoiceActionsController (release/send/write-protection/log).
    lifecycle_actions = ("release",)
    preview_property_names = (
        "documentNumber",
        "status",
        "businessPartnerId",
        "customerNumber",
        "documentDate",
        "deliveryDate",
        "grossAmount",
        "currency",
        "paymentStatus",
        "salesOrderId",
    )
    root_property_aliases = {
        "address": "businessPartnerId",
        "documentStatus": "status",
        "project": "projectId",
        "sales": "salesId",
        "salesOrder": "salesOrderId",
        "costCenterValue": "costCenter",
        "deviatingDebtorAccountNumber": "deviatingDebtorAccountNumber",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "deliveryNoteId": field(
            "reference",
            section="references",
            reference="DeliveryNote",
            renderProperty="documentNumber",
        ),
        "currentAmountPaid": field("decimal", section="financials", access="readOnly"),
        "cashDiscountGranted": field("decimal", section="financials"),
        "dunningLevel": field("select", section="dunning"),
        "dunningDate": field("date", section="dunning"),
        "isDunningBlocked": field("boolean", section="dunning"),
        "dunningInternalComment": field(section="dunning"),
        "isDatevCompleted": field("boolean", section="financials", access="readOnly"),
        "directDebitDate": field("date", section="financials"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "invoiceId": field("reference", reference="SalesInvoice", renderProperty="documentNumber"),
    }
    legacy_root_field_map = {
        "lieferschein": "deliveryNoteId",
        "zahlungsweise": "paymentMethodName",
        "zahlungsstatus": "paymentStatus",
        "ist": "currentAmountPaid",
        "soll": "grossAmount",
        "skonto_gegeben": "cashDiscountGranted",
        "zahlungszieltage": "paymentTargetDays",
        "zahlungszieltageskonto": "paymentTargetDiscountDays",
        "zahlungszielskonto": "paymentTargetDiscount",
        "mahnwesen": "dunningLevel",
        "mahnwesen_datum": "dunningDate",
        "mahnwesen_gesperrt": "isDunningBlocked",
        "mahnwesen_internebemerkung": "dunningInternalComment",
        "datev_abgeschlossen": "isDatevCompleted",
        "einzugsdatum": "directDebitDate",
        "lieferdatum": "deliveryDate",
        "waehrung": "currency",
        "email": "email",
        "telefon": "phone",
        "telefax": "fax",
    }
    legacy_line_item_field_map = {
        "rechnung": "invoiceId",
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
        "erloesefestschreiben": "isRevenueAccountLocked",
        "ohnepreis": "shouldPrintWithoutPrice",
        "ausblenden_im_pdf": "isHiddenOnPdf",
        "skontobetrag": "cashDiscountAmount",
        "steuerbetrag": "taxAmount",
        "umsatz_netto_einzeln": "netRevenueSingle",
        "umsatz_netto_gesamt": "netRevenueTotal",
        "umsatz_brutto_einzeln": "grossRevenueSingle",
        "umsatz_brutto_gesamt": "grossRevenueTotal",
    }
    payload_root_field_map = {
        "deliveryNoteId": "lieferschein",
        "paymentMethodName": "zahlungsweise",
        "paymentStatus": "zahlungsstatus",
        "cashDiscountGranted": "skonto_gegeben",
        "paymentTargetDays": "zahlungszieltage",
        "paymentTargetDiscountDays": "zahlungszieltageskonto",
        "paymentTargetDiscount": "zahlungszielskonto",
        "dunningLevel": "mahnwesen",
        "dunningDate": "mahnwesen_datum",
        "isDunningBlocked": "mahnwesen_gesperrt",
        "dunningInternalComment": "mahnwesen_internebemerkung",
        "directDebitDate": "einzugsdatum",
    }
    list_include = "address,lineItems,customFields,lineItems.customFields,lineItems.product,project,tags,activity"
    detail_include = list_include
    payload_field_map = {
        "masterReferenceNumber": "master_reference_number",
        # customerOrderNumber not remapped: v3 accepts the English name (create+update).
        "deliveryDate": "lieferdatum",
        "useAlternativeDocumentTitle": "abweichendebezeichnung",
        "deviatingDebtorAccountNumber": "kundennummer_buchhaltung",
        "costCenterValue": "kostenstelle",
        "salesOrder": "auftragid",
        "dunningSettings.level": "mahnwesen",
        "dunningSettings.date": "mahnwesen_datum",
        "dunningSettings.blocked": "mahnwesen_gesperrt",
        "dunningSettings.comment": "mahnwesen_internebemerkung",
        "dunningSetManually": "mahnwesenfestsetzen",
        "dunningManualSettings.paymentStatus": "zahlungsstatus",
        "dunningManualSettings.paidAt": "bezahlt_am",
        "dunningManualSettings.actualAmount.amount": "ist",
        "dunningManualSettings.discountGiven.amount": "skonto_gegeben",
    }
    payload_object_fields = {
        "address",
        "project",
        "sales",
        "salesOrder",
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
        "salesOrder": "salesOrder.id",
        "costCenterValue": "costCenter",
        "paymentStatus": "paymentStatus",
        # `masterReferenceNumber` is filtered by its camelCase key on /invoices
        # (the snake_case form is a write-path field name and is rejected as a
        # filter) — leave it to pass through rather than aliasing to snake_case.
        "dunningSettings.date": "mahnwesen_datum",
        "dunningSettings.blocked": "mahnwesen_gesperrt",
        "dunningSettings.comment": "mahnwesen_internebemerkung",
        "dunningSettings.level": "mahnwesen",
        "dunningSetManually": "mahnwesenfestsetzen",
    }

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
            "dunningSettings": p(
                "embedded",
                "Dunning",
                section="dunning",
                properties={
                    "level": p(
                        "select",
                        "Dunning level",
                        options=_dunning_options("en"),
                    ),
                    "date": p("date", "Mahndatum", "Dunning date"),
                    "blocked": p("boolean", "Gesperrt", "Blocked"),
                    "comment": p("string", "Bemerkung", "Comment"),
                },
            ),
            "dunningSetManually": p(
                "boolean",
                "Mahnwesen manuell setzen",
                "Set dunning manually",
                section="dunning",
            ),
            "dunningManualSettings": p(
                "embedded",
                "Manuelle Mahnwerte",
                "Manual dunning settings",
                section="dunning",
                properties={
                    "paymentStatus": p(
                        "select",
                        "Payment status",
                        options=[
                            {"value": "pending", "label": "Pending"},
                            {"value": "partiallyPaid", "label": "Partially paid"},
                            {"value": "paid", "label": "Paid"},
                        ],
                    ),
                    "paidAt": p("date", "Bezahlt am", "Paid at"),
                    "actualAmount": p("embedded", "Istbetrag", "Actual amount"),
                    "discountGiven": p("embedded", "Gewährter Skonto", "Discount given"),
                    "difference": p("embedded", "Differenz", "Difference", access="readOnly"),
                },
            ),
            "deviatingDebtorAccountNumber": p(
                "string",
                "Abweichendes Debitorenkonto",
                "Deviating debtor account number",
                section="references",
            ),
            "effectiveAddresses": p(
                "embedded",
                "Effektive Adressen",
                "Effective addresses",
                access="readOnly",
                section="address",
                properties={
                    "billTo": p("embedded", "Bill to", properties=_postal_address_properties()),
                },
            ),
            "salesOrder": p(
                "reference",
                "Sales order",
                reference="SalesOrder",
                renderProperty="documentNumber",
                section="references",
            ),
        }

    def _extra_line_item_properties(self) -> dict[str, Any]:
        return {}

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
        record["salesOrder"] = ref(record.pop("auftragid", None))
        record["masterReferenceNumber"] = record.pop("master_reference_number", None)
        record["customerOrderNumber"] = record.pop(
            "ihrebestellnummer", record.get("customerOrderNumber")
        )
        record["deliveryDate"] = record.pop("lieferdatum", None)
        record["useAlternativeDocumentTitle"] = record.pop("abweichendebezeichnung", None)
        record["deviatingDebtorAccountNumber"] = record.pop("kundennummer_buchhaltung", None)
        record["costCenterValue"] = record.pop("kostenstelle", None)
        record["paymentStatus"] = record.pop("zahlungsstatus", None)
        record["dunningSettings"] = {
            "level": record.pop("mahnwesen", None),
            "date": record.pop("mahnwesen_datum", None),
            "blocked": record.pop("mahnwesen_gesperrt", None),
            "comment": record.pop("mahnwesen_internebemerkung", None),
        }
        record["dunningSetManually"] = record.pop("mahnwesenfestsetzen", None)
        record["dunningManualSettings"] = {
            "paymentStatus": record.get("paymentStatus"),
            "paidAt": record.pop("bezahlt_am", None),
            "actualAmount": {"amount": record.pop("ist", None)},
            "discountGiven": {"amount": record.pop("skonto_gegeben", None)},
            "difference": record.pop("difference", None),
        }
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
        if key == "dunningSettings" and isinstance(value, dict):
            out: dict[str, Any] = {}
            if "level" in value:
                out["mahnwesen"] = value.get("level")
            if "date" in value:
                out["mahnwesen_datum"] = value.get("date")
            if "blocked" in value:
                out["mahnwesen_gesperrt"] = value.get("blocked")
            if "comment" in value:
                out["mahnwesen_internebemerkung"] = value.get("comment")
            return out
        if key == "dunningManualSettings" and isinstance(value, dict):
            out: dict[str, Any] = {}
            out["mahnwesenfestsetzen"] = True
            if "paymentStatus" in value:
                out["zahlungsstatus"] = value.get("paymentStatus")
            if "paidAt" in value:
                out["bezahlt_am"] = value.get("paidAt")
            if isinstance(value.get("actualAmount"), dict):
                out["ist"] = value["actualAmount"].get("amount")
            if isinstance(value.get("discountGiven"), dict):
                out["skonto_gegeben"] = value["discountGiven"].get("amount")
            return out
        if key == "salesOrder":
            if value in (None, ""):
                return {"auftragid": None}
            if isinstance(value, dict):
                return None
            return {"auftragid": {"id": str(value)}}
        if key in {"address", "project", "sales", "editor"}:
            if value in (None, ""):
                return {self.payload_field_map.get(key, key): None}
            if isinstance(value, dict):
                return None
            return {self.payload_field_map.get(key, key): {"id": str(value)}}
        return None
