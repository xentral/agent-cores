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


def _status_options(*values: str) -> list[dict[str, str]]:
    labels = {
        "draft": "Draft",
        "released": "Released",
        "sent": "Sent",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "accepted": "Accepted",
        "rejected": "Rejected",
        "inProgress": "In progress",
        "started": "Started",
    }
    return [{"value": value, "label": labels.get(value, value)} for value in values]


def _ref(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        result = deepcopy(value)
        if result.get("id") is not None:
            result["id"] = str(result["id"])
        return result
    return {"id": str(value)}


class V3DocumentAdapter(BusinessDocumentAdapterBase):
    base_path: str
    document_number_label = "Document number"
    business_partner_label = "Business partner"
    business_partner_reference = "BusinessPartner"
    business_partner_number_key = "customerNumber"
    business_partner_number_label = "Customer number"
    status_values: tuple[str, ...] = ("draft", "released", "sent", "completed", "cancelled")
    extra_payload_object_fields: set[str] = set()
    extra_payload_read_only_fields: set[str] = set()
    additional_query_aliases: dict[str, str] = {}
    root_property_aliases = {
        "documentStatus": "status",
        "project": "projectId",
        "sales": "salesId",
        "costCenterValue": "costCenter",
    }

    def _preview_property_names(self) -> tuple[str, ...]:
        return (
            "documentNumber",
            self.root_property_aliases.get("documentStatus", "documentStatus"),
            self.root_property_aliases.get("address", "address"),
            self.business_partner_number_key,
            "documentDate",
            "deliveryDate",
            "grossAmount",
            "totalGross",
            "currency",
            "paymentStatus",
        )

    def _root_properties(self) -> dict[str, Any]:
        props = super()._root_properties()
        props["address"]["label"] = self.business_partner_label
        props["address"]["reference"] = self.business_partner_reference
        props["documentNumber"]["label"] = self.document_number_label
        props["documentStatus"]["options"] = _status_options(*self.status_values)

        if self.business_partner_number_key != "customerNumber":
            customer_number = props.pop("customerNumber")
            customer_number["label"] = self.business_partner_number_label
            props[self.business_partner_number_key] = customer_number
        else:
            props["customerNumber"]["label"] = self.business_partner_number_label

        props.update(self._document_extra_root_properties())
        return props

    def _document_extra_root_properties(self) -> dict[str, Any]:
        return {}

    @property
    def payload_object_fields(self) -> set[str]:  # type: ignore[override]
        return (
            BusinessDocumentAdapterBase.payload_object_fields
            | self.extra_payload_object_fields
            | {
                "address",
                "project",
                "sales",
                "editor",
            }
        )

    @property
    def payload_read_only_fields(self) -> set[str]:  # type: ignore[override]
        return (
            BusinessDocumentAdapterBase.payload_read_only_fields
            | self.extra_payload_read_only_fields
        )

    @property
    def query_aliases(self) -> dict[str, str]:  # type: ignore[override]
        return {
            "documentStatus": "status",
            "address": "address.id",
            "documentAddress.name": "documentAddress.name",
            "documentAddress.country": "documentAddress.country",
            "project": "project.id",
            "sales": "sales.id",
            "costCenterValue": "costCenter",
            **self.additional_query_aliases,
        }

    def _record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        record["documentStatus"] = record.pop("status", None)
        record["isWriteProtected"] = record.pop("writeProtection", False)
        record["address"] = _ref(record.pop("address", None))
        record["documentAddress"] = self._postal_address_from_v3(
            record.pop("documentAddress", None),
            record.pop("vatId", None),
        )
        record["project"] = _ref(record.pop("project", None))
        record["sales"] = _ref(record.pop("vertriebid", record.pop("sales", None)))
        record["editor"] = _ref(record.pop("editor", None))
        record["masterReferenceNumber"] = record.pop(
            "masterReferenceNumber",
            record.pop("master_reference_number", None),
        )
        record["costCenterValue"] = record.pop("costCenterValue", record.pop("costCenter", None))
        record["isDocumentSent"] = record.pop("isDocumentSent", record.pop("documentSent", None))
        if isinstance(record.get("lineItems"), list):
            record["lineItems"] = [
                self._line_item_transform(item) if isinstance(item, dict) else item
                for item in record["lineItems"]
            ]
        if record.get("documentAddress") is not None:
            record["effectiveAddresses"] = {
                "billTo": deepcopy(record.get("documentAddress")),
                "shipTo": deepcopy(record.get("documentAddress")),
            }
        return self._document_record_transform(record)

    def _document_record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        return record

    def _payload_nested(self, key: str, value: Any) -> dict[str, Any] | None:
        if key == "documentAddress":
            address, vat_id = self._postal_address_to_v3(value)
            return {"documentAddress": address, **({"vatId": vat_id} if vat_id is not None else {})}
        if key in {"address", "project", "sales", "editor"} | self.extra_payload_object_fields:
            target = self.payload_field_map.get(key, key)
            if value in (None, ""):
                return {target: None}
            if isinstance(value, dict):
                return None
            return {target: {"id": str(value)}}
        return self._document_payload_nested(key, value)

    def _document_payload_nested(self, key: str, value: Any) -> dict[str, Any] | None:
        return None


class OfferAdapter(V3DocumentAdapter):
    manifest = EmulationManifest(
        key="Offer",
        label_en="Offer",
        category="Sales",
        rollout_batch="offer-v1",
        adapter="v3-offer",
        source_apis=("/api/v3/offers",),
        operations=("list", "read", "create", "update", "delete"),
    )
    base_path = "/api/v3/offers"
    # No filter key on /offers (verified against the live allow-list).
    filterable_removals = ("masterReferenceNumber", "validUntil")
    # Grounded in OfferActionsController (release/cancel/send/write-protection/log).
    lifecycle_actions = ("release", "cancel")
    document_number_label = "Offer number"
    root_property_aliases = {
        **V3DocumentAdapter.root_property_aliases,
        "address": "businessPartnerId",
        "salesOrder": "salesOrderId",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "salesOrderDocumentNumber": field(section="references", access="readOnly"),
        "paymentMethodName": field(section="financials"),
        "grossAmount": field("decimal", section="financials", access="readOnly"),
        "desiredDeliveryDate": field("date", section="shipping"),
        "internetOrderNumber": field(section="references"),
        "externalShopOrderId": field(section="references"),
        "preferredWarehouseId": field(
            "reference", section="shipping", reference="Warehouse", renderProperty="name"
        ),
        "hasManualShippingCostApproval": field("boolean", section="shipping"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "alternativePriceText": field(section="financials"),
        "isDeliveryDateAsCalendarWeek": field("boolean", section="shipping"),
    }
    legacy_root_field_map = {
        "auftrag": "salesOrderDocumentNumber",
        "zahlungsweise": "paymentMethodName",
        "gesamtsumme": "grossAmount",
        "lieferdatum": "desiredDeliveryDate",
        "internet": "internetOrderNumber",
        "shopextid": "externalShopOrderId",
        "standardlager": "preferredWarehouseId",
        "keinporto": "hasManualShippingCostApproval",
    }
    legacy_line_item_field_map = {
        "artikel": "productId",
        "projekt": "projectId",
        "preis": "netPrice",
        "waehrung": "currency",
        "status": "status",
        "umsatzsteuer": "salesTaxType",
        "textalternativpreis": "alternativePriceText",
        "ohnepreis": "shouldPrintWithoutPrice",
        "kostenstelle": "costCenter",
    }
    additional_query_aliases = {
        "validUntil": "validUntil",
        "salesOrder": "salesOrder.id",
    }
    extra_payload_object_fields = {"salesOrder"}

    def _preview_property_names(self) -> tuple[str, ...]:
        return (
            "documentNumber",
            "status",
            "businessPartnerId",
            "customerNumber",
            "documentDate",
            "validUntil",
            "desiredDeliveryDate",
            "grossAmount",
            "currency",
            "internetOrderNumber",
        )

    def _document_extra_root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "validUntil": p(
                "date", "Valid until", filterable=True, sortable=True, section="general"
            ),
            "salesOrder": p(
                "reference",
                "Sales order",
                reference="SalesOrder",
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
                    "shipTo": p("embedded", "Ship to", properties=_postal_address_properties()),
                },
            ),
        }

    def _document_record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        record["salesOrder"] = _ref(record.pop("salesOrder", record.pop("auftragid", None)))
        return record


class ProformaInvoiceAdapter(V3DocumentAdapter):
    manifest = EmulationManifest(
        key="ProformaInvoice",
        label_en="Proforma Invoice",
        category="Sales",
        rollout_batch="proforma-invoice-v1",
        adapter="v3-proforma-invoice",
        source_apis=("/api/v3/proformaInvoices",),
        operations=("list", "read", "create", "update", "delete"),
    )
    base_path = "/api/v3/proformaInvoices"
    # No filter key on /proformaInvoices (verified against the live allow-list).
    filterable_removals = ("customerNumber", "masterReferenceNumber", "costCenter")
    # Grounded in ProformaInvoiceActionsController (release/send/write-protection/log).
    lifecycle_actions = ("release",)
    document_number_label = "Proforma invoice number"
    root_property_aliases = {
        **V3DocumentAdapter.root_property_aliases,
        "address": "businessPartnerId",
        "salesOrder": "salesOrderId",
        "deliveryNote": "deliveryNoteId",
        "offer": "offerId",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "salesOrderNumber": field(section="references"),
        "paidAmount": field("decimal", section="financials", access="readOnly"),
        "dunningLevel": field(section="dunning"),
        "dunningDate": field("date", section="dunning"),
        "isDunningBlocked": field("boolean", section="dunning"),
        "isDatevClosed": field("boolean", section="financials", access="readOnly"),
        "deliveryAddressId": field(
            "reference", section="address", reference="DeliveryAddress", renderProperty="name"
        ),
        "deviatingShipToAddress": field("embedded", section="address"),
        "deviatingCustomsToAddress": field("embedded", section="address"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "proformaInvoiceId": field(
            "reference", reference="ProformaInvoice", renderProperty="documentNumber"
        ),
        "taxRatePercentage": field("decimal", section="financials"),
    }
    legacy_root_field_map = {
        "auftrag": "salesOrderNumber",
        "zahlungsstatus": "paymentStatus",
        "ist": "paidAmount",
        "soll": "totalGross",
        "lieferdatum": "deliveryDate",
        "zahlungszieltage": "paymentTargetDays",
        "mahnwesen": "dunningLevel",
        "mahnwesen_datum": "dunningDate",
        "mahnwesen_gesperrt": "isDunningBlocked",
        "datev_abgeschlossen": "isDatevClosed",
        "waehrung": "currency",
        "lieferid": "deliveryAddressId",
        "liefername": "deviatingShipToAddress",
        "verzollungname": "deviatingCustomsToAddress",
    }
    legacy_line_item_field_map = {
        "proformarechnung": "proformaInvoiceId",
        "artikel": "productId",
        "projekt": "projectId",
        "preis": "netPrice",
        "waehrung": "currency",
        "status": "status",
        "steuersatz": "taxRatePercentage",
        "kostenstelle": "costCenterValue",
        "ohnepreis": "shouldPrintWithoutPrice",
    }
    status_values = ("draft", "released", "sent", "completed", "cancelled")
    additional_query_aliases = {
        "salesOrder": "salesOrder.id",
        "deliveryNote": "deliveryNote.id",
        "offer": "offer.id",
    }
    extra_payload_object_fields = {"salesOrder", "deliveryNote", "offer"}

    def _document_extra_root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "salesOrder": p(
                "reference",
                "Sales order",
                reference="SalesOrder",
                renderProperty="documentNumber",
                section="references",
            ),
            "deliveryNote": p(
                "reference",
                "Delivery note",
                reference="DeliveryNote",
                renderProperty="documentNumber",
                section="references",
            ),
            "offer": p(
                "reference",
                "Offer",
                reference="Offer",
                renderProperty="documentNumber",
                section="references",
            ),
            "totals": p("embedded", "Totals", access="readOnly", section="financials"),
        }

    def _document_record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        record["salesOrder"] = _ref(record.pop("salesOrder", record.pop("auftragid", None)))
        record["deliveryNote"] = _ref(
            record.pop("deliveryNote", record.pop("lieferscheinid", None))
        )
        record["offer"] = _ref(record.pop("offer", record.pop("angebotid", None)))
        return record


class ReturnOrderAdapter(V3DocumentAdapter):
    manifest = EmulationManifest(
        key="ReturnOrder",
        label_en="Return Order",
        category="Warehousing",
        rollout_batch="return-order-v1",
        adapter="v3-return-order",
        source_apis=("/api/v3/returnOrders",),
        operations=("list", "read", "create", "update", "delete"),
    )
    base_path = "/api/v3/returnOrders"
    # No filter key on /returnOrders (verified against the live allow-list).
    filterable_removals = ("masterReferenceNumber", "costCenter", "returnReason")
    # Grounded in ReturnOrderActionsController (release/cancel/complete/send/...).
    lifecycle_actions = ("release", "cancel", "complete")
    document_number_label = "Return order number"
    root_property_aliases = {
        **V3DocumentAdapter.root_property_aliases,
        "address": "businessPartnerId",
        "salesOrder": "salesOrderId",
        "deliveryNote": "deliveryNoteId",
        "creditNote": "creditNoteId",
        "progressStatus": "progress",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "deliveryNoteDocumentNumber": field(section="references"),
        "salesOrderDocumentNumber": field(section="references"),
        "invoiceId": field(
            "reference",
            section="references",
            reference="SalesInvoice",
            renderProperty="documentNumber",
        ),
        "replacementSalesOrderId": field(
            "reference",
            section="references",
            reference="SalesOrder",
            renderProperty="documentNumber",
        ),
        "recipientId": field(
            "reference", section="references", reference="BusinessPartner", renderProperty="name"
        ),
        "recipientEmail": field(section="references"),
        "isReturnOrderToSupplier": field("boolean", section="references"),
        "supplierId": field(
            "reference", section="references", reference="BusinessPartner", renderProperty="name"
        ),
        "preferredWarehouseId": field(
            "reference", section="shipping", reference="Warehouse", renderProperty="name"
        ),
        "commissionConsignmentWarehouseId": field(
            "reference", section="shipping", reference="Warehouse", renderProperty="name"
        ),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "returnOrderId": field(
            "reference", reference="ReturnOrder", renderProperty="documentNumber"
        ),
        "deliveryNoteLineItemId": field(
            "reference", reference="DeliveryNoteLineItem", renderProperty="id"
        ),
        "receivedQuantity": field("decimal", access="readOnly"),
        "reimbursementQuantity": field("decimal"),
        "serialNumber": field(),
    }
    legacy_root_field_map = {
        "lieferschein": "deliveryNoteDocumentNumber",
        "auftrag": "salesOrderDocumentNumber",
        "rechnungid": "invoiceId",
        "replacementorder_id": "replacementSalesOrderId",
        "recipient_id": "recipientId",
        "recipient_email": "recipientEmail",
        "lieferantenretoure": "isReturnOrderToSupplier",
        "lieferant": "supplierId",
        "standardlager": "preferredWarehouseId",
        "kommissionskonsignationslager": "commissionConsignmentWarehouseId",
    }
    legacy_line_item_field_map = {
        "retoure": "returnOrderId",
        "artikel": "productId",
        "projekt": "projectId",
        "lieferschein_position_id": "deliveryNoteLineItemId",
        "menge_eingang": "receivedQuantity",
        "menge_gutschrift": "reimbursementQuantity",
        "seriennummer": "serialNumber",
        "ausblenden_im_pdf": "shouldHideOnPdf",
    }
    status_values = ("draft", "released", "inProgress", "completed", "cancelled")
    additional_query_aliases = {
        "salesOrder": "salesOrder.id",
        "deliveryNote": "deliveryNote.id",
        "creditNote": "creditNote.id",
        "returnReason": "returnReason",
        "progressStatus": "progressStatus",
    }
    extra_payload_object_fields = {"salesOrder", "deliveryNote", "creditNote"}
    extra_payload_read_only_fields = {"progressStatus"}

    def _preview_property_names(self) -> tuple[str, ...]:
        return (
            "documentNumber",
            "status",
            "progress",
            "businessPartnerId",
            "customerNumber",
            "documentDate",
            "salesOrderId",
            "deliveryNoteId",
            "invoiceId",
            "preferredWarehouseId",
        )

    def _document_extra_root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "progressStatus": p(
                "select",
                "Progress status",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
                options=_status_options(
                    "draft", "released", "inProgress", "completed", "cancelled"
                ),
            ),
            "returnReason": p("string", "Return reason", filterable=True, section="references"),
            "salesOrder": p(
                "reference",
                "Sales order",
                reference="SalesOrder",
                renderProperty="documentNumber",
                section="references",
            ),
            "deliveryNote": p(
                "reference",
                "Delivery note",
                reference="DeliveryNote",
                renderProperty="documentNumber",
                section="references",
            ),
            "creditNote": p(
                "reference",
                "Credit note",
                reference="SalesCreditNote",
                renderProperty="documentNumber",
                section="references",
            ),
        }

    def _document_record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        record["salesOrder"] = _ref(record.pop("salesOrder", record.pop("auftragid", None)))
        record["deliveryNote"] = _ref(
            record.pop("deliveryNote", record.pop("lieferscheinid", None))
        )
        record["creditNote"] = _ref(record.pop("creditNote", record.pop("gutschriftid", None)))
        record["returnReason"] = record.pop("returnReason", record.pop("reason", None))
        record["progressStatus"] = record.pop("progressStatus", record.pop("progress", None))
        return record


class PriceInquiryAdapter(V3DocumentAdapter):
    manifest = EmulationManifest(
        key="PriceInquiry",
        label_en="Price Inquiry",
        category="Purchasing",
        rollout_batch="price-inquiry-v1",
        adapter="v3-price-inquiry",
        source_apis=("/api/v3/priceInquiries",),
        operations=("list", "read", "create", "update", "delete"),
    )
    base_path = "/api/v3/priceInquiries"
    # No PriceInquiry action controller exists in Xentral — expose no actions.
    supports_send = False
    supports_write_protection = False
    supports_log_activity = False
    document_number_label = "Price inquiry number"
    root_property_aliases = {
        **V3DocumentAdapter.root_property_aliases,
        "address": "businessPartnerId",
        "purchaseOrder": "purchaseOrderId",
        "requestedDeliveryDate": "deliveryDate",
    }
    csv_root_properties = {
        **DOCUMENT_COMMON_ROOT,
        "salesOrderId": field(
            "reference",
            section="references",
            reference="SalesOrder",
            renderProperty="documentNumber",
        ),
        "salesOrderNumber": field(section="references", access="readOnly"),
        "shippingMethod": field(section="shipping"),
    }
    csv_line_item_properties = {
        **LINE_ITEM_COMMON,
        "priceInquiryId": field(
            "reference", reference="PriceInquiry", renderProperty="documentNumber"
        ),
    }
    legacy_root_field_map = {
        "auftragid": "salesOrderId",
        "auftrag": "salesOrderNumber",
        "versandart": "shippingMethod",
        "waehrung": "currency",
    }
    legacy_line_item_field_map = {
        "preisanfrage": "priceInquiryId",
        "artikel": "productId",
        "projekt": "projectId",
        "preis": "netPrice",
    }
    business_partner_label = "Supplier"
    business_partner_reference = "Supplier"
    business_partner_number_key = "supplierNumber"
    business_partner_number_label = "Supplier number"
    additional_query_aliases = {
        "supplierNumber": "supplierNumber",
        "purchaseOrder": "purchaseOrder.id",
    }
    extra_payload_object_fields = {"purchaseOrder"}

    def _preview_property_names(self) -> tuple[str, ...]:
        return (
            "documentNumber",
            "status",
            "businessPartnerId",
            "supplierNumber",
            "documentDate",
            "deliveryDate",
            "salesOrderNumber",
            "purchaseOrderId",
            "currency",
        )

    def _document_extra_root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        return {
            "purchaseOrder": p(
                "reference",
                "Purchase order",
                reference="PurchaseOrder",
                renderProperty="documentNumber",
                section="references",
            ),
            "requestedDeliveryDate": p(
                "date",
                "Requested delivery date",
                filterable=True,
                sortable=True,
                section="general",
            ),
        }

    def _document_record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        record["supplierNumber"] = record.pop(
            "supplierNumber", record.pop("lieferantennummer", None)
        )
        record["purchaseOrder"] = _ref(
            record.pop("purchaseOrder", record.pop("bestellungid", None))
        )
        record["requestedDeliveryDate"] = record.pop(
            "requestedDeliveryDate",
            record.pop("gewuenschteslieferdatum", None),
        )
        return record


class ProductionOrderAdapter(V3DocumentAdapter):
    manifest = EmulationManifest(
        key="ProductionOrder",
        label_en="Production Order",
        category="Production",
        rollout_batch="production-order-v1",
        adapter="v3-production",
        source_apis=("/api/v3/productions",),
        operations=("list", "read", "create", "delete"),
    )
    base_path = "/api/v3/productions"
    # Grounded in ProductionActionsController: release/start/logActivity only —
    # no cancel/complete, no send, no write protection. ``release`` here takes no
    # request body (ReleaseProductionAction, unlike ReleaseBusinessDocumentData).
    lifecycle_actions = ("release", "start")
    release_has_document_date = False
    supports_send = False
    supports_write_protection = False
    document_number_label = "Production number"
    root_property_aliases = {
        **V3DocumentAdapter.root_property_aliases,
        "product": "productId",
        "warehouse": "warehouseId",
    }
    status_values = ("draft", "released", "started", "completed", "cancelled")
    # /api/v3/productions has no filter key for these (verified against the live
    # allow-list — it exposes preferredWarehouse.id / salesOrder.id, not a
    # product / warehouse / quantity / cost-center / master-reference filter).
    filterable_removals = (
        "productId",
        "warehouseId",
        "quantity",
        "costCenter",
        "masterReferenceNumber",
    )
    # /api/v3/productions rejects a bare ``product`` include (allowed:
    # ``materials.product``); use that so even a plain list works.
    list_include = "materials.product,project,tags,activity"
    detail_include = "materials.product,project,tags,activity,files"
    extra_payload_object_fields = {"product", "warehouse"}
    extra_payload_read_only_fields = {"lineItems"}
    additional_query_aliases = {
        "productionNumber": "productionNumber",
        "product": "product.id",
        "warehouse": "warehouse.id",
    }

    def _root_properties(self) -> dict[str, Any]:
        props = super()._root_properties()
        props.pop("address", None)
        props.pop("customerNumber", None)
        props.pop("documentAddress", None)
        props.pop("documentAddressName", None)
        props.pop("country", None)
        props.pop("sales", None)
        props.pop("deliveryTerms", None)
        props.pop("lineItems", None)
        props["documentNumber"]["label"] = "Production number"
        props["product"] = _property(
            "reference",
            "Product",
            reference="Product",
            renderProperty="name",
            section="general",
            filterable=True,
            previewable=True,
            rules=["required"],
        )
        props["warehouse"] = _property(
            "reference",
            "Warehouse",
            reference="Warehouse",
            renderProperty="name",
            section="references",
            filterable=True,
        )
        props["quantity"] = _property("decimal", "Quantity", section="general", filterable=True)
        props["startedAt"] = _property(
            "datetime", "Started at", access="readOnly", section="general"
        )
        props["completedAt"] = _property(
            "datetime", "Completed at", access="readOnly", section="general"
        )
        props["billOfMaterials"] = _property(
            "collection",
            "Bill of materials",
            access="readOnly",
            section="lineItems",
            node={"properties": self._line_item_properties()},
        )
        return props

    def _document_record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        record["documentNumber"] = record.pop("productionNumber", record.get("documentNumber"))
        record["product"] = _ref(record.pop("product", None))
        record["warehouse"] = _ref(record.pop("warehouse", None))
        record["billOfMaterials"] = record.pop("billOfMaterials", record.pop("lineItems", None))
        return record
