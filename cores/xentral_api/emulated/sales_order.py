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


def _traffic_light_id_options() -> list[dict[str, str]]:
    return [
        {
            "value": "stock",
            "label": "Stock / delivery availability",
            "description": (
                "Stock traffic light for the sales order. Use together with "
                "isStockOk, isPartialDeliveryPossible and line-item stockAvailability* fields."
            ),
        },
        {"value": "payment", "label": "Payment", "description": "Payment-state traffic light."},
        {"value": "vat", "label": "VAT", "description": "VAT/tax validation traffic light."},
        {
            "value": "creditLimit",
            "label": "Credit limit",
            "description": "Customer credit-limit traffic light.",
        },
        {
            "value": "deliveryBlock",
            "label": "Delivery block",
            "description": "Manual/system delivery-block traffic light.",
        },
        {
            "value": "addressValidation",
            "label": "Address validation",
            "description": "Shipping/billing address validation traffic light.",
        },
        {
            "value": "production",
            "label": "Production",
            "description": "Production-related traffic light.",
        },
    ]


def _traffic_light_type_options() -> list[dict[str, str]]:
    return [
        {"value": "system", "label": "System", "description": "Built-in Xentral traffic light."},
        {"value": "custom", "label": "Custom", "description": "Tenant-defined traffic light."},
    ]


def _traffic_light_state_options() -> list[dict[str, str]]:
    return [
        {
            "value": "true",
            "label": "OK / green",
            "description": "Positive traffic-light state for the given type.",
        },
        {
            "value": "false",
            "label": "Not OK / red",
            "description": "Negative traffic-light state for the given type.",
        },
        {
            "value": "partial",
            "label": "Partial / yellow",
            "description": "Partial state. For type=stock this means partially deliverable.",
        },
        {
            "value": "notExisting",
            "label": "Not present",
            "description": "The underlying check/data point is not present.",
        },
        {
            "value": "unpaid",
            "label": "Unpaid",
            "description": "Payment traffic-light state.",
        },
        {
            "value": "partiallyPaid",
            "label": "Partially paid",
            "description": "Payment traffic-light state.",
        },
        {
            "value": "fullyPaid",
            "label": "Fully paid",
            "description": "Payment traffic-light state.",
        },
    ]


class SalesOrderAdapter(BusinessDocumentAdapterBase):
    manifest = EmulationManifest(
        key="SalesOrder",
        label_en="Sales Order",
        category="Sales",
        rollout_batch="sales-order-v2",
        adapter="v3-sales-order",
        source_apis=("/api/v3/salesOrders",),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v3/salesOrders"
    # Grounded in SalesOrderActionsController (v3) + the legacy Fulfillment
    # SalesOrderController (v1-beta dispatch / createPartialSalesOrder).
    lifecycle_actions = ("release", "cancel", "complete")
    supports_dispatch = True
    supports_create_partial_sales_order = True
    preview_property_names = (
        "documentNumber",
        "status",
        "businessPartnerId",
        "customerNumber",
        "customerOrderNumber",
        "externalOrderNumber",
        "documentDate",
        "deliveryDate",
        "totalGrossAmount",
        "isStockOk",
    )
    root_property_aliases = {
        "address": "businessPartnerId",
        "documentStatus": "status",
        "project": "projectId",
        "sales": "salesId",
        "costCenterValue": "costCenter",
        "preferredWarehouse": "preferredWarehouseId",
        "commissionConsignmentWarehouse": "commissionConsignmentWarehouseId",
        "salesChannel": "salesChannelId",
        "autoDispatch": "shouldAutoDispatch",
        "vatChecked": "isVatChecked",
        "manualPaymentApproval": "hasManualPaymentApproval",
        "manualShippingCostApproval": "hasManualShippingCostApproval",
        "manualDeliveryBlockApproval": "hasManualDeliveryBlockApproval",
        "disableCancellationEmail": "shouldDisableCancellationEmail",
        "disableTrackingEmail": "shouldDisableTrackingEmail",
        "disablePaymentEmail": "shouldDisablePaymentEmail",
        "useAlternativeDocumentTitle": "shouldUseAlternativeDocumentTitle",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "externalOrderNumber": field(section="references"),
        "externalOrderId": field(section="references"),
        "externalOrderStatus": field(section="references", access="readOnly"),
        "isStockOk": field(
            "boolean",
            section="shipping",
            access="readOnly",
            label="Stock fully available",
            description=(
                "Order-level stock check. true means the order is stock-coverable; "
                "false means not fully stock-coverable. Use isPartialDeliveryPossible, "
                "trafficLights[id=stock] and lineItems.stockAvailability* to distinguish "
                "partial delivery from no delivery."
            ),
        ),
        "isStockReserved": field(
            "boolean",
            section="shipping",
            access="readOnly",
            label="Stock reserved",
            description="Whether stock has been reserved for this sales order.",
        ),
        "stockAvailableFifo": field(
            "select",
            section="shipping",
            access="readOnly",
            label="Stock availability FIFO",
            description=(
                "Order-level delivery availability calculated against FIFO stock. "
                "Prefer this over product stock lookups when deciding whether a sales order is deliverable. "
                "Treat the value as an opaque Xentral availability enum unless the tenant metadata exposes options."
            ),
        ),
        "stockAvailableOpenSupply": field(
            "select",
            section="shipping",
            access="readOnly",
            label="Stock availability incl. open supply",
            description=(
                "Order-level delivery availability including open purchase/production supply. "
                "Use together with stockAvailableFifo to explain whether current stock or incoming supply covers the order. "
                "Treat the value as an opaque Xentral availability enum unless the tenant metadata exposes options."
            ),
        ),
        "isShippingCostOk": field("boolean", section="shipping", access="readOnly"),
        "isDeliveryBlockOk": field("boolean", section="shipping", access="readOnly"),
        "isPartialDeliveryPossible": field(
            "boolean",
            section="shipping",
            label="Partial delivery possible",
            description=(
                "Order-level fulfillment signal. true means Xentral considers a partial delivery possible; "
                "false means no partial delivery should be proposed unless a human overrides it."
            ),
        ),
        "parentSalesOrderId": field(
            "reference",
            section="references",
            reference="SalesOrder",
            renderProperty="documentNumber",
        ),
        "partialDeliveryNumber": field("integer", section="references"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "salesOrderId": field("reference", reference="SalesOrder", renderProperty="documentNumber"),
        "isReorderedViaExternalPurchase": field("boolean"),
        "isDeliveryDateAsCalendarWeek": field("boolean"),
        "subProjectId": field("integer", reference="Project", renderProperty="name"),
        "potentialDeliveryDate": field(
            "date",
            section="shipping",
            label="Potential delivery date",
            description="Earliest known delivery date for this line item based on current stock/supply planning.",
        ),
        "stockAvailabilityOpenSupply": field(
            "select",
            section="shipping",
            access="readOnly",
            label="Line stock availability incl. open supply",
            description=(
                "Line-level delivery availability including open purchase/production supply. "
                "Use for deciding which positions can be shipped later or partially. Treat the value as an "
                "opaque Xentral availability enum unless the tenant metadata exposes options."
            ),
        ),
        "stockAvailabilityFifo": field(
            "select",
            section="shipping",
            access="readOnly",
            label="Line stock availability FIFO",
            description=(
                "Line-level delivery availability against current FIFO stock. "
                "Use for deciding whether this position can be shipped from current stock. Treat the value as an "
                "opaque Xentral availability enum unless the tenant metadata exposes options."
            ),
        ),
    }
    legacy_root_field_map = {
        "internet": "externalOrderNumber",
        "shopextid": "externalOrderId",
        "shopextstatus": "externalOrderStatus",
        "ustid": "vatId",
        "ust_befreit": "taxation",
        "zahlungsweise": "paymentMethod",
        "zahlungszieltage": "paymentTargetDays",
        "zahlungszieltageskonto": "paymentTargetDiscountDays",
        "zahlungszielskonto": "paymentTargetDiscount",
        "versendet_am": "sentAt",
        "versendet_per": "sentVia",
        "versendet_durch": "sentBy",
        "gesamtsumme": "totalGrossAmount",
        "lager_ok": "isStockOk",
        "is_stock_available_fifo": "stockAvailableFifo",
        "is_stock_available_opensupply": "stockAvailableOpenSupply",
        "porto_ok": "isShippingCostOk",
        "vorkasse_ok": "paymentStatus",
        "reserviert_ok": "isStockReserved",
        "deckungsbeitragcalc": "isContributionMarginCalculated",
        "deckungsbeitrag": "contributionMargin",
        "erloes_netto": "netProfit",
        "umsatz_netto": "netRevenue",
        "teillieferung_moeglich": "isPartialDeliveryPossible",
        "teillieferungvon": "parentSalesOrderId",
        "teillieferungnummer": "partialDeliveryNumber",
        "ihrebestellnummer": "customerOrderNumber",
        "rabatt": "discount",
        "waehrung": "currency",
        "pdfarchiviert": "pdfArchiveCount",
        "pdfarchiviertversion": "pdfArchiveVersion",
        "lieferid": "deliveryAddressId",
    }
    legacy_line_item_field_map = {
        "auftrag": "salesOrderId",
        "artikel": "productId",
        "projekt": "projectId",
        "preis": "netPrice",
        "waehrung": "currency",
        "status": "status",
        "umsatzsteuer": "salesTaxType",
        "kostenstelle": "costCenter",
        "erloese": "revenueAccountValue",
        "einkaufspreiswaehrung": "purchasePriceCurrency",
        "einkaufspreisurspruenglich": "originalPurchasePrice",
        "einkaufspreisid": "purchasePriceId",
        "erloesefestschreiben": "isRevenueAccountLocked",
        "ohnepreis": "shouldPrintWithoutPrice",
        "ausblenden_im_pdf": "isHiddenOnPdf",
        "skontobetrag": "cashDiscountAmount",
        "steuerbetrag": "taxAmount",
        "umsatz_netto_einzeln": "netRevenueItemSingle",
        "umsatz_netto_gesamt": "netRevenueItemTotal",
        "umsatz_brutto_einzeln": "grossRevenueItemSingle",
        "umsatz_brutto_gesamt": "grossRevenueItemTotal",
        "stock_availability_open_supply": "stockAvailabilityOpenSupply",
        "stock_availability_fifo": "stockAvailabilityFifo",
    }
    payload_root_field_map = {
        "externalOrderNumber": "internet",
        "externalOrderId": "shopextid",
        "vatId": "ustid",
        "taxation": "ust_befreit",
        "paymentMethod": "zahlungsweise",
        "paymentTargetDays": "zahlungszieltage",
        "paymentTargetDiscountDays": "zahlungszieltageskonto",
        "paymentTargetDiscount": "zahlungszielskonto",
        # customerOrderNumber is intentionally NOT remapped: the v3 write DTO
        # accepts the English name and persists it (verified live against mvp),
        # whereas sending the German `ihrebestellnummer` made v3 silently drop it.
        "discount": "rabatt",
        "currency": "waehrung",
    }
    list_include = (
        "lineItems,lineItems.product,lineItems.customFields,customFields,project,address,tags,"
        "__internal__trafficLights,activity"
    )
    detail_include = (
        "lineItems,lineItems.product,lineItems.customFields,customFields,project,address,tags,"
        "__internal__trafficLights,activity,lineItems.customFields"
    )
    payload_field_map = {
        "deviatingShipToAddress": "deviatingDeliveryAddress",
        "masterReferenceNumber": "master_reference_number",
        "externalOrderNumber": "internet",
        "transactionNumber": "transaktionsnummer",
        "externalOrderId": "shopextid",
        # Date fields are NOT remapped: the v3 write DTO (API-733) accepts the
        # English names and persists them (verified live against mvp on 26.30.1);
        # sending the German column names made v3 silently drop them.
        "storageCountry": "storage_country",
        "useAlternativeDocumentTitle": "abweichendebezeichnung",
        "autoDispatch": "autoversand",
        "vatChecked": "ust_ok",
        "createDocuments": "art",
        "manualPaymentApproval": "vorabbezahltmarkieren",
        "manualShippingCostApproval": "keinporto",
        "manualDeliveryBlockApproval": "lieferungtrotzsperre",
        "fastLane": "fastlane",
        "disableCancellationEmail": "keinestornomail",
        "disableTrackingEmail": "keinetrackingmail",
        "disablePaymentEmail": "zahlungsmailcounter",
        "deviatingDebtorAccountNumber": "kundennummer_buchhaltung",
        "salesChannel": "shop",
        "preferredWarehouse": "standardlager",
        "commissionConsignmentWarehouse": "kommissionskonsignationslager",
        "shippingMethod": "versandart",
    }
    payload_object_fields = {
        "address",
        "project",
        "sales",
        "preferredWarehouse",
        "commissionConsignmentWarehouse",
        "shippingMethod",
        "salesChannel",
        "editor",
        "standardlager",
        "kommissionskonsignationslager",
        "versandart",
        "shop",
    }
    payload_read_only_fields = BusinessDocumentAdapterBase.payload_read_only_fields | {
        "effectiveAddresses",
    }
    query_aliases = {
        "documentStatus": "status",
        "address": "address.id",
        "businessPartnerId": "address.id",
        "documentAddress.name": "documentAddress.name",
        "documentAddress.country": "documentAddress.country",
        # Flat overview `country` (filled from documentAddress) must resolve to the
        # v3 nested filter key — otherwise the raw `country` filter is rejected.
        "country": "documentAddress.country",
        # Emulated postal-address field names that differ from the v3 filter keys.
        "documentAddress.postalCode": "documentAddress.zipCode",
        "documentAddress.additionalAddressInformation": "documentAddress.addressSupplement",
        # Old-GUI order filters that live under a v3 sub-object.
        "paymentMethod": "financials.paymentMethod.id",
        "currency": "totals.net.currency",
        "totalGrossAmount": "totals.gross.amount",
        "project": "project.id",
        "projectId": "project.id",
        "sales": "sales.id",
        "salesChannel": "salesChannel.id",
        "preferredWarehouse": "preferredWarehouse.id",
        "commissionConsignmentWarehouse": "commissionConsignmentWarehouse.id",
        "shippingMethod": "shippingMethod.id",
        "costCenterValue": "costCenter",
        "storageCountry": "storage_country",
        "autoDispatch": "autoversand",
        "vatChecked": "ust_ok",
        "createDocuments": "art",
        "manualPaymentApproval": "vorabbezahltmarkieren",
        "manualShippingCostApproval": "keinporto",
        "manualDeliveryBlockApproval": "lieferungtrotzsperre",
        "fastLane": "fastlane",
        "disableCancellationEmail": "keinestornomail",
        "disableTrackingEmail": "keinetrackingmail",
        "disablePaymentEmail": "zahlungsmailcounter",
    }

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        meta = super().metadata(accept_language)
        properties = meta["rootNode"]["properties"]
        for key in ("businessPartnerId", "projectId"):
            properties[key]["filterable"] = True
        for key in ("salesId", "masterReferenceNumber"):
            properties[key].pop("filterable", None)
        # Expose the filters the classic order-list GUI offered. Flat business
        # fields resolve to their v3 filter key via ``query_aliases``; the
        # documentAddress sub-fields are filterable on the v3 list endpoint and
        # pass through under their ``documentAddress.<field>`` path (with two
        # renamed via aliases: postalCode→zipCode, additional…→addressSupplement).
        for key in (
            "vatId",
            "customerOrderNumber",
            "internalComment",
            "paymentMethod",
            "currency",
            "totalGrossAmount",
            "shippingMethod",
        ):
            if key in properties:
                properties[key]["filterable"] = True
        address = properties.get("documentAddress", {}).get("properties", {})
        for key in (
            "name",
            "contactPerson",
            "department",
            "subDepartment",
            "street",
            "postalCode",
            "city",
            "country",
            "additionalAddressInformation",
        ):
            if key in address:
                address[key]["filterable"] = True

        # Re-annotate operators now that this adapter has added its own filterable
        # fields (the base already ran once for the base-declared ones). The
        # shared helper resolves paymentMethod/shippingMethod (→ `.id`) to the id
        # operator set and documentDate/totalGrossAmount to the comparison set.
        self._annotate_filter_operators(properties)
        return meta

    def _extra_root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "deviatingShipToAddress": p(
                "embedded",
                "Deviating ship-to address",
                section="address",
                properties=_postal_address_properties(),
            ),
            "preferredWarehouse": p(
                "reference",
                "Preferred warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="shipping",
            ),
            "commissionConsignmentWarehouse": p(
                "reference",
                "Commission/consignment warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="shipping",
            ),
            "shippingMethod": p(
                "reference",
                "Shipping method",
                reference="ShippingMethod",
                renderProperty="name",
                section="shipping",
            ),
            # Fulfillment dates — writable on v3 since API-733 (26.30.1). Declared
            # here so the core surfaces them on read (write already passes through).
            "desiredDeliveryDate": p("date", "Desired delivery date", section="shipping"),
            "earliestFulfillmentDate": p("date", "Earliest fulfillment date", section="shipping"),
            "reservationDate": p("date", "Reservation date", section="shipping"),
            "salesChannel": p(
                "reference",
                "Sales channel",
                reference="SalesChannel",
                renderProperty="name",
                section="references",
            ),
            "storageCountry": p(
                "string",
                "Storage country",
                section="shipping",
            ),
            "useAlternativeDocumentTitle": p(
                "boolean",
                "Use alternative document title",
                section="content",
            ),
            "autoDispatch": p("boolean", "Auto dispatch", section="shipping"),
            "vatChecked": p("boolean", "VAT checked", section="shipping"),
            "createDocuments": p(
                "select",
                "Documents to create",
                section="shipping",
                options=[
                    {"value": "deliveryNote", "label": "Delivery note"},
                    {"value": "invoice", "label": "Invoice"},
                    {
                        "value": "deliveryNoteAndInvoice",
                        "label": "Delivery note + invoice",
                    },
                ],
            ),
            "manualPaymentApproval": p(
                "boolean",
                "Manual payment approval",
                section="shipping",
            ),
            "manualShippingCostApproval": p(
                "boolean",
                "Manual shipping cost approval",
                section="shipping",
            ),
            "manualDeliveryBlockApproval": p(
                "boolean",
                "Manual delivery block approval",
                section="shipping",
            ),
            "fastLane": p("boolean", "Fast lane", section="shipping"),
            "isStockOk": p(
                "boolean",
                "Bestand vollständig verfügbar",
                "Stock fully available",
                access="readOnly",
                section="shipping",
                description=(
                    "Order-level stock check. true means the order is fully deliverable from the "
                    "availability check; false means it is not fully deliverable. Use "
                    "isPartialDeliveryPossible and trafficLights[id=stock] to distinguish partial "
                    "delivery from no delivery."
                ),
            ),
            "isStockReserved": p(
                "boolean",
                "Bestand reserviert",
                "Stock reserved",
                access="readOnly",
                section="shipping",
                description="Whether stock has been reserved for this sales order.",
            ),
            "isPartialDeliveryPossible": p(
                "boolean",
                "Teillieferung möglich",
                "Partial delivery possible",
                section="shipping",
                description=(
                    "Order-level fulfillment signal. true means Xentral considers a partial delivery "
                    "possible. Prefer this field when looking for partial-delivery candidates."
                ),
            ),
            "stockAvailableFifo": p(
                "select",
                "Lieferbarkeit FIFO",
                "Stock availability FIFO",
                access="readOnly",
                section="shipping",
                description=(
                    "Order-level availability against current FIFO stock. Prefer this over product "
                    "stock lookups when deciding whether the order can be shipped now. Treat the value "
                    "as an opaque Xentral availability enum unless the tenant metadata exposes options."
                ),
            ),
            "stockAvailableOpenSupply": p(
                "select",
                "Lieferbarkeit inkl. Zulauf",
                "Stock availability incl. open supply",
                access="readOnly",
                section="shipping",
                description=(
                    "Order-level availability including open purchase/production supply. Use together "
                    "with stockAvailableFifo to explain whether current stock or incoming supply covers the order. "
                    "Treat the value as an opaque Xentral availability enum unless the tenant metadata exposes options."
                ),
            ),
            "disableCancellationEmail": p(
                "boolean",
                "Storno-Mail deaktivieren",
                "Disable cancellation email",
                section="shipping",
            ),
            "disableTrackingEmail": p(
                "boolean",
                "Tracking-Mail deaktivieren",
                "Disable tracking email",
                section="shipping",
            ),
            "disablePaymentEmail": p(
                "boolean", "Zahlungs-Mail deaktivieren", "Disable payment email", section="shipping"
            ),
            "deviatingDebtorAccountNumber": p(
                "string",
                "Deviating debtor account number",
                section="references",
            ),
            "documentStatus": p(
                "select",
                "Document status",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
                options=_status_options("en"),
            ),
            "effectiveAddresses": p(
                "embedded",
                "Effective addresses",
                access="readOnly",
                section="address",
                properties={
                    "soldTo": p("embedded", "Sold to", properties=_postal_address_properties()),
                    "shipTo": p("embedded", "Ship to", properties=_postal_address_properties()),
                },
            ),
            "directDebitDate": p("date", "Einzugsdatum", "Direct debit date", section="financials"),
            "trafficLights": p(
                "collection",
                "Ampeln",
                "Traffic lights",
                access="readOnly",
                section="shipping",
                description=(
                    "System and custom traffic lights for this sales order. For delivery decisions, "
                    "look for id='stock'. state=true means stock OK/green, state='partial' means "
                    "partially deliverable/yellow, state='false' means not stock-coverable/red, and "
                    "state='notExisting' means no signal. Use with isStockOk, isPartialDeliveryPossible "
                    "and line-item stockAvailability* fields; do not infer delivery availability from "
                    "unrelated traffic-light types."
                ),
                # System status lights (stock, vat, payment, …). Xentral mixes
                # bool and string states (true / "partial" / "fullyPaid" /
                # "notExisting"), so ``state`` is typed as a string.
                node={
                    "properties": {
                        "id": p(
                            "select",
                            "ID",
                            description="Traffic-light identifier. For delivery availability, use id='stock'.",
                            options=_traffic_light_id_options(),
                        ),
                        "type": p(
                            "select",
                            "Typ",
                            "Type",
                            description="Traffic-light category. System traffic lights usually have type='system'; use id='stock' for delivery availability.",
                            options=_traffic_light_type_options(),
                        ),
                        "state": p(
                            "select",
                            "Status",
                            "State",
                            description=(
                                "Traffic-light state for the selected light. For id='stock': "
                                "true=fully deliverable, partial=partially deliverable, "
                                "false=not deliverable, notExisting=no stock signal."
                            ),
                            options=_traffic_light_state_options(),
                        ),
                    }
                },
            ),
        }

    def _extra_line_item_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "stockAvailabilityFifo": p(
                "select",
                "Lieferbarkeit FIFO",
                "Stock availability FIFO",
                access="readOnly",
                section="shipping",
                description=(
                    "Line-level availability against current FIFO stock. Use this for current-stock "
                    "delivery decisions instead of looking up product stock separately. Treat the value "
                    "as an opaque Xentral availability enum unless the tenant metadata exposes options."
                ),
            ),
            "stockAvailabilityOpenSupply": p(
                "select",
                "Lieferbarkeit inkl. Zulauf",
                "Stock availability incl. open supply",
                access="readOnly",
                section="shipping",
                description=(
                    "Line-level availability including open purchase/production supply. Use this to "
                    "explain later deliverability or pending supply. Treat the value as an opaque "
                    "Xentral availability enum unless the tenant metadata exposes options."
                ),
            ),
            "potentialDeliveryDate": p(
                "date",
                "Mögliches Lieferdatum",
                "Potential delivery date",
                section="shipping",
                description="Earliest known date this line can potentially be delivered.",
            ),
            "externalNumber": p("string", "Externe Nummer", "External number", section="general"),
            "hasChildLineItems": p(
                "boolean",
                "Hat Unterpositionen",
                "Has child line items",
                access="readOnly",
                section="general",
            ),
            "parentLineItem": p(
                "reference",
                "Übergeordnete Position",
                "Parent line item",
                reference="LineItem",
                renderProperty="id",
                section="general",
            ),
            "desiredQualityControlAttributes": p(
                "embedded",
                "Qualitätsmerkmale",
                "Quality control attributes",
                section="general",
                properties={
                    "bestBeforeDate": p("date", "Mindesthaltbarkeitsdatum", "Best before date"),
                    "batch": p("string", "Charge", "Batch"),
                },
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
        deviating = record.pop("deviatingDeliveryAddress", None)
        record["deviatingShipToAddress"] = self._postal_address_from_v3(deviating)
        record["project"] = ref(record.pop("project", None))
        record["sales"] = ref(record.pop("vertriebid", None))
        record["preferredWarehouse"] = ref(record.pop("standardlager", None))
        record["commissionConsignmentWarehouse"] = ref(
            record.pop("kommissionskonsignationslager", None)
        )
        # v3 returns this as an English {id,name} object; fall back to the legacy
        # German key only when present, else keep what v3 sent (a None default
        # would clobber the real reference with null).
        record["shippingMethod"] = ref(record.pop("versandart", record.get("shippingMethod")))
        record["salesChannel"] = ref(record.pop("shop", None))
        record["masterReferenceNumber"] = record.pop("master_reference_number", None)
        record["storageCountry"] = record.pop("storage_country", None)
        record["useAlternativeDocumentTitle"] = record.pop("abweichendebezeichnung", None)
        record["autoDispatch"] = record.pop("autoversand", None)
        record["vatChecked"] = record.pop("ust_ok", None)
        record["createDocuments"] = record.pop("art", None)
        record["manualPaymentApproval"] = record.pop("vorabbezahltmarkieren", None)
        record["manualShippingCostApproval"] = record.pop("keinporto", None)
        record["manualDeliveryBlockApproval"] = record.pop("lieferungtrotzsperre", None)
        record["fastLane"] = record.pop("fastlane", None)
        record["disableCancellationEmail"] = record.pop("keinestornomail", None)
        record["disableTrackingEmail"] = record.pop("keinetrackingmail", None)
        record["disablePaymentEmail"] = record.pop("zahlungsmailcounter", None)
        record["deviatingDebtorAccountNumber"] = record.pop("kundennummer_buchhaltung", None)
        # v3 now returns this under its English name; only fall back to the
        # legacy German key when present, else keep the value v3 already sent
        # (popping with a None default would clobber the real value with null).
        record["customerOrderNumber"] = record.pop(
            "ihrebestellnummer", record.get("customerOrderNumber")
        )
        record["externalOrderNumber"] = record.pop("internet", None)
        record["transactionNumber"] = record.pop("transaktionsnummer", None)
        record["externalOrderId"] = record.pop("shopextid", None)
        # v3 (26.30.1+) returns these under their English names; only fall back to
        # the legacy German columns when present, else keep the v3 value (a None
        # default would clobber the real value with null).
        record["desiredDeliveryDate"] = record.pop("lieferdatum", record.get("desiredDeliveryDate"))
        record["desiredDeliveryDateAsCalendarWeek"] = record.pop(
            "lieferdatumkw", record.get("desiredDeliveryDateAsCalendarWeek")
        )
        record["earliestFulfillmentDate"] = record.pop(
            "tatsaechlicheslieferdatum", record.get("earliestFulfillmentDate")
        )
        record["reservationDate"] = record.pop("reservationdate", record.get("reservationDate"))
        record["directDebitDate"] = record.pop("einzugsdatum", None)
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
            record["effectiveAddresses"] = {"soldTo": sold_to, "shipTo": ship_to}
        return record

    def _payload_nested(self, key: str, value: Any) -> dict[str, Any] | None:
        if key == "documentAddress":
            address, vat_id = self._postal_address_to_v3(value)
            return {"documentAddress": address, **({"vatId": vat_id} if vat_id is not None else {})}
        if key == "deviatingShipToAddress":
            address, _ = self._postal_address_to_v3(value)
            return {"deviatingDeliveryAddress": address}
        if key in {
            "address",
            "project",
            "sales",
            "preferredWarehouse",
            "commissionConsignmentWarehouse",
            "shippingMethod",
            "salesChannel",
            "editor",
        }:
            if value in (None, ""):
                return {self.payload_field_map.get(key, key): None}
            if isinstance(value, dict):
                return None
            return {self.payload_field_map.get(key, key): {"id": str(value)}}
        return None
