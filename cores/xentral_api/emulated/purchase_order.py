from __future__ import annotations

from copy import deepcopy
from typing import Any

from entity_registry.core_sdk import EmulationManifest
from .business_document import (
    BusinessDocumentAdapterBase,
    _base_line_item_properties,
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


def _confirmation_options(language: str) -> list[dict[str, str]]:
    confirmations = [
        ("internet", "Internet", "Internet"),
        ("email", "E-Mail", "Email"),
        ("telephone", "Telefon", "Telephone"),
        ("telefax", "Telefax", "Fax"),
        ("letter", "Brief", "Letter"),
        ("other", "Sonstige", "Other"),
    ]
    return [{"value": value, "label": en} for value, de, en in confirmations]


class PurchaseOrderAdapter(BusinessDocumentAdapterBase):
    manifest = EmulationManifest(
        key="PurchaseOrder",
        label_en="Purchase Order",
        category="Purchasing",
        rollout_batch="purchase-order-v1",
        adapter="v3-purchase-order",
        source_apis=("/api/v3/purchaseOrders",),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v3/purchaseOrders"
    # Grounded in PurchaseOrderActionsController (release/cancel/complete/send/...).
    lifecycle_actions = ("release", "cancel", "complete")
    preview_property_names = (
        "documentNumber",
        "status",
        "businessPartnerId",
        "supplierNumber",
        "documentDate",
        "confirmedDeliveryDate",
        "desiredDeliveryDate",
        "supplierOrderNumber",
        "supplierOfferNumber",
        "currency",
    )
    root_property_aliases = {
        "address": "businessPartnerId",
        "documentStatus": "status",
        "project": "projectId",
        "priceInquiry": "priceInquiryId",
        "shippingMethod": "shippingMethodId",
        "costCenter": "costCenter",
        "useAlternativeDocumentTitle": "shouldUseAlternativeDocumentTitle",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "deliveryDate": field("date", section="shipping"),
        "customerNumberAtSupplier": field(section="references"),
        "shouldPrintWithoutPrices": field("boolean", section="content"),
        "shouldPrintWithoutProductText": field("boolean", section="content"),
        "shouldShowTax": field("boolean", section="financials"),
        "cashDiscount": field("decimal", section="financials", access="readOnly"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "purchaseOrderId": field(
            "reference", reference="PurchaseOrder", renderProperty="documentNumber"
        ),
        "selectedQuantity": field("decimal"),
        "priceInquiryLineItemId": field(
            "reference", reference="PriceInquiryLineItem", renderProperty="id"
        ),
    }
    legacy_root_field_map = {
        "lieferdatum": "deliveryDate",
        "kundennummerlieferant": "customerNumberAtSupplier",
        "bestellungohnepreis": "shouldPrintWithoutPrices",
        "ohne_artikeltext": "shouldPrintWithoutProductText",
        "anzeigesteuer": "shouldShowTax",
        "skontobetrag": "cashDiscount",
        "zahlungsweise": "paymentMethod",
        "zahlungsstatus": "paymentStatus",
        "zahlungszieltage": "paymentTargetDays",
        "zahlungszieltageskonto": "paymentTargetDiscountDays",
        "zahlungszielskonto": "paymentTargetDiscount",
        "waehrung": "currency",
    }
    legacy_line_item_field_map = {
        "bestellung": "purchaseOrderId",
        "artikel": "productId",
        "projekt": "projectId",
        "preis": "netPrice",
        "waehrung": "currency",
        "status": "status",
        "umsatzsteuer": "salesTaxType",
        "kostenstelle": "costCenter",
        "erloese": "revenueAccountValue",
        "erloesefestschreiben": "isRevenueAccountLocked",
        "skontobetrag": "cashDiscount",
        "preisanfrage_position_id": "priceInquiryLineItemId",
        "auswahlmenge": "selectedQuantity",
    }
    list_include = "lineItems,lineItems.product,lineItems.customFields,customFields,project,address,tags,activity"
    detail_include = list_include
    payload_field_map = {
        "supplierNumber": "lieferantennummer",
        "confirmedDeliveryDate": "bestaetigteslieferdatum",
        "desiredDeliveryDate": "gewuenschteslieferdatum",
        "useAlternativeDocumentTitle": "abweichendebezeichnung",
        "costCenter": "kostenstelle",
        "confirmationType": "bestellungbestaetigtper",
        "isConfirmed": "bestellung_bestaetigt",
        "supplierOrderNumber": "supplier_order_number",
        "supplierOfferNumber": "supplier_offer_number",
    }
    payload_object_fields = {
        "address",
        "project",
        "shippingMethod",
        "priceInquiry",
        "editor",
    }
    payload_read_only_fields = BusinessDocumentAdapterBase.payload_read_only_fields | {
        "supplierNumber",
        "supplierOrderNumber",
        "supplierOfferNumber",
        "isConfirmed",
        "effectiveAddresses",
    }
    query_aliases = {
        "documentStatus": "status",
        "address": "address.id",
        "documentAddress.name": "documentAddress.name",
        "documentAddress.country": "documentAddress.country",
        "project": "project.id",
        # These filter on their camelCase key on /purchaseOrders (the snake_case /
        # German forms are write-path names and are rejected as filters), so let
        # them pass through unaliased: costCenter, confirmedDeliveryDate,
        # desiredDeliveryDate, supplierOrderNumber, supplierOfferNumber.
        "useAlternativeDocumentTitle": "abweichendebezeichnung",
        "confirmationType": "bestellungbestaetigtper",
        "isConfirmed": "bestellung_bestaetigt",
        "shippingMethod": "versandart.id",
        "priceInquiry": "priceInquiry.id",
    }
    # customerNumber / sales / masterReferenceNumber / supplierNumber have no
    # filter key on /purchaseOrders (verified against the live allow-list).
    filterable_removals = ("customerNumber", "sales", "masterReferenceNumber", "supplierNumber")
    line_item_create_fields = BusinessDocumentAdapterBase.line_item_create_fields | {
        "price",
        "taxRate",
        "effectiveTaxRate",
        "taxLegalNotice",
        "supplierProductNumber",
        "supplierProductName",
    }
    line_item_update_fields = BusinessDocumentAdapterBase.line_item_update_fields | {
        "price",
        "taxRate",
        "effectiveTaxRate",
        "taxLegalNotice",
        "supplierProductNumber",
        "supplierProductName",
    }

    def _root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        props = super()._root_properties()
        props.pop("costCenterValue", None)
        props["supplierNumber"] = p(
            "string",
            "Lieferantennummer",
            "Supplier number",
            access="readOnly",
            filterable=True,
            searchable=True,
            section="general",
        )
        props["confirmedDeliveryDate"] = p(
            "date",
            "Bestätigtes Lieferdatum",
            "Confirmed delivery date",
            filterable=True,
            sortable=True,
            section="general",
        )
        props["desiredDeliveryDate"] = p(
            "date",
            "Gewünschtes Lieferdatum",
            "Desired delivery date",
            filterable=True,
            sortable=True,
            section="general",
        )
        props["useAlternativeDocumentTitle"] = p(
            "boolean",
            "Abweichende Bezeichnung verwenden",
            "Use alternative document title",
            section="content",
        )
        props["confirmationType"] = p(
            "select",
            "Bestätigt per",
            "Confirmation type",
            section="references",
            options=_confirmation_options("en"),
        )
        props["isConfirmed"] = p(
            "boolean",
            "Bestätigt",
            "Confirmed",
            access="readOnly",
            section="general",
        )
        props["shippingMethod"] = p(
            "reference",
            "Versandart",
            "Shipping method",
            reference="ShippingMethod",
            renderProperty="name",
            section="shipping",
        )
        props["supplierOrderNumber"] = p(
            "string",
            "Lieferanten-Bestellnummer",
            "Supplier order number",
            access="readOnly",
            filterable=True,
            section="references",
        )
        props["supplierOfferNumber"] = p(
            "string",
            "Lieferanten-Angebotsnummer",
            "Supplier offer number",
            access="readOnly",
            filterable=True,
            section="references",
        )
        props["priceInquiry"] = p(
            "reference",
            "Preisanfrage",
            "Price inquiry",
            reference="PriceInquiry",
            renderProperty="documentNumber",
            access="readOnly",
            section="references",
        )
        props["costCenter"] = p(
            "string",
            "Kostenstelle",
            "Cost center",
            filterable=True,
            section="references",
        )
        props["financials"] = p(
            "embedded",
            "Finanzen",
            "Financials",
            section="financials",
            properties={
                "paymentMethod": p(
                    "reference",
                    "Payment method",
                    access="readOnly",
                    reference="PaymentMethod",
                    renderProperty="name",
                ),
                "paymentTerms": p(
                    "embedded",
                    "Payment terms",
                    access="readOnly",
                    properties={
                        "paymentTargetDays": p(
                            "integer", "Zahlungsziel in Tagen", "Payment target days"
                        ),
                        "paymentTargetDiscount": p("decimal", "Skonto", "Payment target discount"),
                        "paymentTargetDiscountDays": p(
                            "integer",
                            "Skonto-Tage",
                            "Payment target discount days",
                        ),
                    },
                ),
                "tax": p(
                    "embedded",
                    "Tax",
                    properties={
                        "taxation": p(
                            "select",
                            "Versteuerung",
                            "Taxation",
                            options=[
                                {"value": "domestic", "label": "Domestic"},
                                {"value": "eu", "label": "EU"},
                                {"value": "export", "label": "Export"},
                                {"value": "exempt", "label": "Exempt"},
                            ],
                        ),
                        "taxRates": p(
                            "embedded",
                            "Steuersätze",
                            "Tax rates",
                            access="readOnly",
                            properties={
                                "standard": p("decimal", "Regelsatz", "Standard"),
                                "reduced": p("decimal", "Ermäßigter Satz", "Reduced"),
                            },
                        ),
                    },
                ),
                "currency": p("string", "Währung", "Currency"),
                "exchangeRate": p("decimal", "Wechselkurs", "Exchange rate"),
            },
        )
        props["totals"] = p(
            "embedded",
            "Summen",
            "Totals",
            access="readOnly",
            section="financials",
            properties={
                "gross": p("embedded", "Brutto", "Gross"),
            },
        )
        props["printSettings"] = p(
            "embedded",
            "Druckeinstellungen",
            "Print settings",
            section="content",
            properties={
                "withoutLetterhead": p("boolean", "Ohne Briefpapier", "Without letterhead"),
                "withoutProductText": p("boolean", "Ohne Artikeltext", "Without product text"),
                "withoutPrices": p("boolean", "Ohne Preise", "Without prices"),
                "withoutProductNumbers": p(
                    "boolean", "Ohne Produktnummern", "Without product numbers"
                ),
                "isConfirmationRequestVisible": p(
                    "boolean",
                    "Bestätigungsanfrage anzeigen",
                    "Show confirmation request",
                ),
            },
        )
        props["deviatingShipToAddress"] = p(
            "embedded",
            "Abweichende Lieferadresse",
            "Deviating ship-to address",
            section="address",
            properties=_postal_address_properties(),
        )
        props["effectiveAddresses"] = p(
            "embedded",
            "Effektive Adressen",
            "Effective addresses",
            access="readOnly",
            section="address",
            properties={
                "billTo": p("embedded", "Bill to", properties=_postal_address_properties()),
                "shipTo": p("embedded", "Ship to", properties=_postal_address_properties()),
            },
        )
        props["lineItems"] = p(
            "collection",
            "Line items",
            section="lineItems",
            node={"properties": self._line_item_properties()},
        )
        return props

    def _line_item_properties(self) -> dict[str, Any]:
        props = {
            **_base_line_item_properties(),
            **self._csv_properties(self.csv_line_item_properties),
        }
        props["supplierProductNumber"] = _property(
            "string",
            "Supplier product number",
            section="general",
        )
        props["supplierProductName"] = _property(
            "string",
            "Supplier product name",
            section="general",
        )
        return self._aliased_properties(props, self.line_item_property_aliases)

    def _line_item_transform(self, item: dict[str, Any]) -> dict[str, Any]:
        item = super()._line_item_transform(item)
        if "supplier_product_number" in item and "supplierProductNumber" not in item:
            item["supplierProductNumber"] = item.pop("supplier_product_number")
        if "supplier_product_name" in item and "supplierProductName" not in item:
            item["supplierProductName"] = item.pop("supplier_product_name")
        if "tax_legal_notice" in item and "taxLegalNotice" not in item:
            item["taxLegalNotice"] = item.pop("tax_legal_notice")
        return item

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
        record["supplierNumber"] = record.pop(
            "supplierNumber", record.pop("lieferantennummer", None)
        )
        record["confirmedDeliveryDate"] = record.pop(
            "confirmedDeliveryDate", record.pop("bestaetigteslieferdatum", None)
        )
        record["desiredDeliveryDate"] = record.pop(
            "desiredDeliveryDate", record.pop("gewuenschteslieferdatum", None)
        )
        record["useAlternativeDocumentTitle"] = record.pop(
            "useAlternativeDocumentTitle", record.pop("abweichendebezeichnung", None)
        )
        record["confirmationType"] = record.pop(
            "confirmationType", record.pop("bestellungbestaetigtper", None)
        )
        record["isConfirmed"] = record.pop("isConfirmed", record.pop("bestellung_bestaetigt", None))
        record["shippingMethod"] = ref(record.pop("shippingMethod", record.pop("versandart", None)))
        record["supplierOrderNumber"] = record.pop(
            "supplierOrderNumber", record.pop("supplier_order_number", None)
        )
        record["supplierOfferNumber"] = record.pop(
            "supplierOfferNumber", record.pop("supplier_offer_number", None)
        )
        record["priceInquiry"] = ref(record.pop("priceInquiry", None))
        record["costCenter"] = record.pop("costCenter", record.pop("kostenstelle", None))

        financials = record.pop("financials", None)
        if not isinstance(financials, dict):
            financials = {}
        payment_method = financials.pop("paymentMethod", record.pop("paymentMethod", None))
        payment_terms = financials.pop("paymentTerms", record.pop("paymentTerms", None))
        tax = financials.pop("tax", record.pop("tax", None))
        currency = financials.pop("currency", record.pop("currency", None))
        exchange_rate = financials.pop("exchangeRate", record.pop("exchangeRate", None))
        financial_payload: dict[str, Any] = {}
        if payment_method is not None:
            financial_payload["paymentMethod"] = ref(payment_method)
        if isinstance(payment_terms, dict):
            financial_payload["paymentTerms"] = {
                "paymentTargetDays": payment_terms.get("paymentTargetDays"),
                "paymentTargetDiscount": payment_terms.get("paymentTargetDiscount"),
                "paymentTargetDiscountDays": payment_terms.get("paymentTargetDiscountDays"),
            }
        if isinstance(tax, dict):
            tax_rates = tax.get("taxRates") if isinstance(tax.get("taxRates"), dict) else {}
            financial_payload["tax"] = {
                "taxation": tax.get("taxation"),
                "taxRates": {
                    "standard": tax_rates.get("standard") if isinstance(tax_rates, dict) else None,
                    "reduced": tax_rates.get("reduced") if isinstance(tax_rates, dict) else None,
                },
            }
        if currency is not None:
            financial_payload["currency"] = currency
        if exchange_rate is not None:
            financial_payload["exchangeRate"] = exchange_rate
        if financial_payload:
            record["financials"] = financial_payload

        print_settings = record.pop("printSettings", None)
        if not isinstance(print_settings, dict):
            print_settings = {}
        if any(
            key in print_settings
            for key in (
                "withoutLetterhead",
                "withoutProductText",
                "withoutPrices",
                "withoutProductNumbers",
                "isConfirmationRequestVisible",
            )
        ) or any(
            key in record
            for key in (
                "without_letterhead",
                "without_product_text",
                "without_prices",
                "without_product_numbers",
                "bestellbestaetigung",
            )
        ):
            record["printSettings"] = {
                "withoutLetterhead": print_settings.get(
                    "withoutLetterhead", record.pop("without_letterhead", None)
                ),
                "withoutProductText": print_settings.get(
                    "withoutProductText", record.pop("without_product_text", None)
                ),
                "withoutPrices": print_settings.get(
                    "withoutPrices", record.pop("without_prices", None)
                ),
                "withoutProductNumbers": print_settings.get(
                    "withoutProductNumbers", record.pop("without_product_numbers", None)
                ),
                "isConfirmationRequestVisible": print_settings.get(
                    "isConfirmationRequestVisible", record.pop("bestellbestaetigung", None)
                ),
            }

        if isinstance(record.get("lineItems"), list):
            record["lineItems"] = [
                self._line_item_transform(item) if isinstance(item, dict) else item
                for item in record["lineItems"]
            ]
        if (
            record.get("documentAddress") is not None
            or record.get("deviatingShipToAddress") is not None
        ):
            sold_to = deepcopy(record.get("documentAddress"))
            ship_to = deepcopy(
                record.get("deviatingShipToAddress") or record.get("documentAddress")
            )
            record["effectiveAddresses"] = {"billTo": sold_to, "shipTo": ship_to}
        return record

    def _payload_nested(self, key: str, value: Any) -> dict[str, Any] | None:
        if key == "documentAddress":
            address, vat_id = self._postal_address_to_v3(value)
            return {"documentAddress": address, **({"vatId": vat_id} if vat_id is not None else {})}
        if key == "deviatingShipToAddress":
            address, _ = self._postal_address_to_v3(value)
            return {"deviatingDeliveryAddress": address}
        if key == "financials" and isinstance(value, dict):
            out: dict[str, Any] = {}
            if "paymentMethod" in value:
                payment_method = value.get("paymentMethod")
                if payment_method in (None, ""):
                    out["paymentMethod"] = None
                elif isinstance(payment_method, dict):
                    out["paymentMethod"] = payment_method
                else:
                    out["paymentMethod"] = {"id": str(payment_method)}
            if "paymentTerms" in value and isinstance(value.get("paymentTerms"), dict):
                terms = value["paymentTerms"]
                out["paymentTerms"] = {
                    "paymentTargetDays": terms.get("paymentTargetDays"),
                    "paymentTargetDiscount": terms.get("paymentTargetDiscount"),
                    "paymentTargetDiscountDays": terms.get("paymentTargetDiscountDays"),
                }
            if "tax" in value and isinstance(value.get("tax"), dict):
                tax = value["tax"]
                tax_rates = tax.get("taxRates") if isinstance(tax.get("taxRates"), dict) else {}
                out["tax"] = {
                    "taxation": tax.get("taxation"),
                    "taxRates": {
                        "standard": tax_rates.get("standard")
                        if isinstance(tax_rates, dict)
                        else None,
                        "reduced": tax_rates.get("reduced")
                        if isinstance(tax_rates, dict)
                        else None,
                    },
                }
            if "currency" in value:
                out["currency"] = value.get("currency")
            if "exchangeRate" in value:
                out["exchangeRate"] = value.get("exchangeRate")
            return {"financials": out}
        if key == "printSettings" and isinstance(value, dict):
            out: dict[str, Any] = {}
            if "withoutLetterhead" in value:
                out["without_letterhead"] = value.get("withoutLetterhead")
            if "withoutProductText" in value:
                out["without_product_text"] = value.get("withoutProductText")
            if "withoutPrices" in value:
                out["without_prices"] = value.get("withoutPrices")
            if "withoutProductNumbers" in value:
                out["without_product_numbers"] = value.get("withoutProductNumbers")
            if "isConfirmationRequestVisible" in value:
                out["bestellbestaetigung"] = value.get("isConfirmationRequestVisible")
            return {"printSettings": out}
        if key in {"address", "project", "shippingMethod", "priceInquiry", "editor"}:
            if value in (None, ""):
                return {self.payload_field_map.get(key, key): None}
            if isinstance(value, dict):
                return None
            target = self.payload_field_map.get(key, key)
            if key == "shippingMethod":
                return {target: {"id": str(value)}}
            if key == "priceInquiry":
                return {target: {"id": str(value)}}
            return {target: {"id": str(value)}}
        return None
