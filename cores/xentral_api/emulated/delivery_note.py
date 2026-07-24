from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest
from .business_document import (
    build_document_actions,
    build_process_steps,
    execute_document_action,
)
from .csv_contract import DOCUMENT_COMMON_ROOT, LINE_ITEM_COMMON, field

_TIMEOUT_SECONDS = 60.0
_ROOT_ALIASES = {
    "documentStatus": "status",
    "associatedAddress": "businessPartnerId",
    "postalAddress": "documentAddress",
    "project": "projectId",
    "sales": "salesId",
    "salesOrder": "salesOrderId",
    "invoice": "invoiceId",
    "orderPicking": "orderPickingId",
    "preferredWarehouse": "preferredWarehouseId",
    "commissionConsignmentWarehouse": "commissionConsignmentWarehouseId",
    "costCenterValue": "costCenter",
    "useAlternativeDocumentTitle": "shouldUseAlternativeDocumentTitle",
}
_LINE_ITEM_ALIASES = {
    "product": "productId",
    "sort": "order",
    "deliveryDateAsCalendarWeek": "shouldShowDeliveryDateAsCalendarWeek",
    "unit": "legacyUnitOfMeasure",
    "parentLineItem": "parentLineItemId",
    "salesOrderLineItem": "salesOrderLineItemId",
    "printSettings": "printSettings",
}
_CSV_ROOT_PROPERTIES = {
    **DOCUMENT_COMMON_ROOT,
    "salesOrderDocumentNumber": field(section="references", previewable=True),
    "shippingMethodName": field(section="shipping", previewable=True),
    "supplierId": field(
        "reference", section="references", reference="BusinessPartner", renderProperty="name"
    ),
    "supplierReturnInfo": field(section="references"),
    "hasNoInvoice": field("boolean", section="financials"),
    "parentDeliveryNoteId": field(
        "reference", section="references", reference="DeliveryNote", renderProperty="documentNumber"
    ),
    "deliveryNoteSplitNumber": field("integer", section="references"),
}
_CSV_LINE_ITEM_PROPERTIES = {
    **LINE_ITEM_COMMON,
    "deliveryNoteId": field("reference", reference="DeliveryNote", renderProperty="documentNumber"),
    "serialNumber": field(),
    "salesOrderLineItemId": field("reference", reference="SalesOrderLineItem", renderProperty="id"),
    "isBilled": field("boolean", section="financials", access="readOnly"),
    "storageText": field(section="shipping"),
}
_LEGACY_ROOT_FIELD_MAP = {
    "auftrag": "salesOrderDocumentNumber",
    "versandart": "shippingMethodName",
    "versendet_am": "sentAt",
    "versendet_per": "sentVia",
    "versendet_durch": "sentBy",
    "lieferant": "supplierId",
    "lieferantenretoureinfo": "supplierReturnInfo",
    "keinerechnung": "hasNoInvoice",
    "teillieferungvon": "parentDeliveryNoteId",
    "teillieferungnummer": "deliveryNoteSplitNumber",
    "pdfarchiviert": "pdfArchiveCount",
    "pdfarchiviertversion": "pdfArchivedVersion",
    "email": "email",
    "telefon": "phone",
    "telefax": "fax",
}
_LEGACY_LINE_ITEM_FIELD_MAP = {
    "lieferschein": "deliveryNoteId",
    "artikel": "productId",
    "projekt": "projectId",
    "seriennummer": "serialNumber",
    "auftrag_position_id": "salesOrderLineItemId",
    "abgerechnet": "isBilled",
    "lagertext": "storageText",
    "ausblenden_im_pdf": "shouldHideOnPdf",
}


def _property(
    type_: str,
    *labels: str,
    **extra: Any,
) -> dict[str, Any]:
    label = labels[-1] if labels else ""
    return {"type": type_, "label": label, **extra}


def _postal_address_properties() -> dict[str, Any]:
    return {
        "name": _property("string", "Name"),
        "department": _property("string", "Department"),
        "subDepartment": _property("string", "Sub-department"),
        "street": _property("string", "Street"),
        "additionalAddressInformation": _property("string", "Additional address"),
        "contactPerson": _property("string", "Contact person"),
        "postalCode": _property("string", "Postal code"),
        "city": _property("string", "City"),
        "country": _property("string", "Country"),
        "vatId": _property("string", "VAT ID"),
    }


def _apply_aliases(data: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    for old_key, new_key in aliases.items():
        if old_key in data and new_key not in data:
            data[new_key] = data.pop(old_key)
    return data


def _line_item_properties() -> dict[str, Any]:
    language = "en"
    return {
        "id": _property("string", "ID", "ID", language=language, access="readOnly"),
        "uuid": _property("string", "UUID", "UUID", language=language, access="readOnly"),
        "type": _property("string", "Typ", "Type", language=language, access="readOnly"),
        "sort": _property("integer", "Sort order"),
        "number": _property(
            "string", "Artikelnummer", "Product number", language=language, access="readOnly"
        ),
        "name": _property("string", "Name"),
        "description": _property("string", "Description"),
        "quantity": _property("decimal", "Quantity"),
        "deliveredQuantity": _property(
            "decimal", "Gelieferte Menge", "Delivered quantity", language=language
        ),
        "unit": _property("string", "Unit"),
        "deliveryDate": _property("date", "Lieferdatum", "Delivery date", language=language),
        "deliveryDateAsCalendarWeek": _property(
            "boolean",
            "Lieferdatum als Kalenderwoche",
            "Delivery date as calendar week",
            language=language,
        ),
        "internalComment": _property(
            "string", "Interner Kommentar", "Internal comment", language=language
        ),
        "customerProductNumber": _property(
            "string", "Kundenartikelnummer", "Customer product number", language=language
        ),
        "countryOfOrigin": _property(
            "string", "Herkunftsland", "Country of origin", language=language
        ),
        "hsCode": _property("string", "Zolltarifnummer", "HS code", language=language),
        "packagingUnit": _property(
            "string", "Verpackungseinheit", "Packaging unit", language=language, access="readOnly"
        ),
        "hasChildLineItems": _property(
            "boolean",
            "Hat Unterpositionen",
            "Has child line items",
            language=language,
            access="readOnly",
        ),
        "product": _property(
            "reference",
            "Artikel",
            "Product",
            language=language,
            reference="Product",
            renderProperty="name",
            rules=["required"],
        ),
        "printSettings": _property(
            "embedded",
            "Druckeinstellungen",
            "Print settings",
            language=language,
            properties={
                "hidden": _property(
                    "boolean",
                    "Auf PDF ausblenden",
                    "Hide on PDF",
                    language=language,
                )
            },
        ),
        "customFields": _property(
            "collection",
            "Custom Fields",
            "Custom fields",
            language=language,
            access="readOnly",
            node={
                "properties": {
                    "key": _property(
                        "string", "Schlüssel", "Key", language=language, access="readOnly"
                    ),
                    "label": _property(
                        "string",
                        "Bezeichnung",
                        "Label",
                        language=language,
                        access="readOnly",
                    ),
                    "value": _property(
                        "string", "Wert", "Value", language=language, access="readOnly"
                    ),
                }
            },
        ),
        "createdAt": _property(
            "datetime", "Erstellt am", "Created at", language=language, access="readOnly"
        ),
        "updatedAt": _property(
            "datetime", "Geändert am", "Updated at", language=language, access="readOnly"
        ),
    }


class DeliveryNoteAdapter:
    """Business-Entity facade over the existing Delivery Note V3 API."""

    manifest = EmulationManifest(
        key="DeliveryNote",
        label_en="Delivery Note",
        category="Warehousing",
        rollout_batch="delivery-note-v2",
        adapter="v3-delivery-note",
        source_apis=("/api/v3/deliveryNotes",),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v3/deliveryNotes"
    # Grounded in DeliveryNoteActionsController (release/cancel/complete/send/...).
    lifecycle_actions = ("release", "cancel", "complete")
    release_has_document_date = True
    supports_send = True
    supports_write_protection = True
    supports_log_activity = True
    supports_dispatch = False
    supports_create_partial_sales_order = False
    supports_tags = True

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        language = "en"
        p = lambda type_, *labels, **extra: _property(  # noqa: E731
            type_, *labels, language=language, **extra
        )
        statuses = [
            ("draft", "Entwurf", "Draft"),
            ("released", "Freigegeben", "Released"),
            ("sent", "Versendet", "Sent"),
            ("cancelled", "Storniert", "Cancelled"),
            ("completed", "Abgeschlossen", "Completed"),
        ]
        properties: dict[str, Any] = {
            "uuid": p("string", "UUID", "UUID", access="readOnly"),
            "id": p("string", "ID", "ID", access="readOnly"),
            "documentNumber": p(
                "string",
                "Lieferscheinnummer",
                "Delivery note number",
                access="readOnly",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            "documentStatus": p(
                "select",
                "Belegstatus",
                "Document status",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
                options=[
                    {"value": value, "label": de if language == "de" else en}
                    for value, de, en in statuses
                ],
            ),
            "documentDate": p(
                "date",
                "Belegdatum",
                "Document date",
                filterable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            # Name + country the document itself stored (its documentAddress
            # snapshot), surfaced as flat overview columns — same as every other
            # document. Distinct from the businessPartnerId master reference.
            "documentAddressName": p(
                "string", "Name", "Name", access="readOnly", previewable=True, section="address"
            ),
            "country": p(
                "string", "Land", "Country", access="readOnly", previewable=True, section="address"
            ),
            "isWriteProtected": p(
                "boolean",
                "Schreibgeschützt",
                "Write protected",
                access="readOnly",
                section="general",
            ),
            "isDocumentSent": p(
                "boolean",
                "Versendet",
                "Document sent",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
            ),
            "customerNumber": p(
                "string",
                "Kundennummer",
                "Customer number",
                access="readOnly",
                filterable=True,
                searchable=True,
                previewable=True,
                section="general",
            ),
            "customerOrderNumber": p(
                "string",
                "Kundenbestellnummer",
                "Customer order number",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
            ),
            "associatedAddress": p(
                "reference",
                "Geschäftspartner",
                "Business partner",
                reference="BusinessPartner",
                renderProperty="name",
                filterable=True,
                previewable=True,
                section="general",
                rules=["required"],
                description=(
                    "Beim Anlegen die Xentral-Adress-ID angeben."
                    if language == "de"
                    else "Enter the Xentral address ID when creating the document."
                ),
            ),
            "postalAddress": p(
                "embedded",
                "Lieferadresse",
                "Shipping address",
                section="address",
                properties=_postal_address_properties(),
            ),
            "project": p(
                "reference",
                "Projekt",
                "Project",
                reference="Project",
                renderProperty="name",
                filterable=True,
                section="references",
            ),
            "sales": p(
                "reference",
                "Vertrieb",
                "Sales",
                reference="BusinessPartner",
                renderProperty="name",
                filterable=True,
                section="references",
            ),
            "salesOrder": p(
                "reference",
                "Auftrag",
                "Sales order",
                reference="SalesOrder",
                renderProperty="documentNumber",
                filterable=True,
                previewable=True,
                section="references",
            ),
            "orderPicking": p(
                "reference",
                "Kommissionierung",
                "Order picking",
                reference="OrderPicking",
                filterable=True,
                section="references",
            ),
            "invoice": p(
                "reference",
                "Rechnung",
                "Invoice",
                access="readOnly",
                reference="SalesInvoice",
                renderProperty="documentNumber",
                section="references",
            ),
            "preferredWarehouse": p(
                "reference",
                "Bevorzugtes Lager",
                "Preferred warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="shipping",
            ),
            "commissionConsignmentWarehouse": p(
                "reference",
                "Kommissions-/Konsignationslager",
                "Commission/consignment warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="shipping",
            ),
            "shippingMethod": p(
                "reference",
                "Versandart",
                "Shipping method",
                access="readOnly",
                reference="ShippingMethod",
                renderProperty="name",
                section="shipping",
            ),
            "masterReferenceNumber": p(
                "string",
                "Master-Referenznummer",
                "Master reference number",
                filterable=True,
                section="shipping",
                rules=["max:255"],
            ),
            "useAlternativeDocumentTitle": p(
                "boolean",
                "Abweichende Bezeichnung verwenden",
                "Use alternative document title",
                access="readOnly",
                section="content",
            ),
            "isDeliveryNoteToSupplier": p(
                "boolean",
                "Lieferantenlieferschein",
                "Delivery note to supplier",
                access="readOnly",
                section="general",
            ),
            "supplierNumber": p(
                "string",
                "Lieferantennummer",
                "Supplier number",
                access="readOnly",
                section="general",
            ),
            "costCenterValue": p(
                "string",
                "Kostenstelle",
                "Cost center",
                filterable=True,
                section="references",
            ),
            "language": p("string", "Sprache", "Language", section="content"),
            "internalDesignation": p(
                "string", "Interne Bezeichnung", "Internal designation", section="content"
            ),
            "bodyIntroduction": p(
                "string", "Einleitungstext", "Body introduction", section="content"
            ),
            "bodyOutroduction": p("string", "Schlusstext", "Body outroduction", section="content"),
            "deliveryTerms": p("string", "Lieferbedingungen", "Delivery terms", section="shipping"),
            "internalComment": p(
                "string", "Interne Bemerkung", "Internal comment", section="content"
            ),
            "printSettings": p(
                "embedded",
                "Druckeinstellungen",
                "Print settings",
                section="content",
                properties={
                    "withoutLetterhead": p(
                        "boolean",
                        "Ohne Briefpapier",
                        "Without letterhead",
                    ),
                    "withoutProductText": p(
                        "boolean",
                        "Ohne Artikeltext",
                        "Without product text",
                    ),
                },
            ),
            "tags": p("tag", "Tags", "Tags", section="content", filterable=True),
            "customFields": p(
                "collection",
                "Custom Fields",
                "Custom fields",
                access="readOnly",
                section="customFields",
                node={
                    "properties": {
                        "key": p("string", "Schlüssel", "Key", access="readOnly"),
                        "label": p("string", "Bezeichnung", "Label", access="readOnly"),
                        "value": p("string", "Wert", "Value", access="readOnly"),
                    }
                },
            ),
            "lineItems": p(
                "collection",
                "Positionen",
                "Line items",
                section="lineItems",
                node={
                    "properties": {
                        **_apply_aliases(_line_item_properties(), _LINE_ITEM_ALIASES),
                        **_CSV_LINE_ITEM_PROPERTIES,
                    }
                },
            ),
            "createdAt": p(
                "datetime", "Erstellt am", "Created at", access="readOnly", sortable=True
            ),
            "updatedAt": p(
                "datetime", "Geändert am", "Updated at", access="readOnly", sortable=True
            ),
        }
        properties.update(_CSV_ROOT_PROPERTIES)
        properties = _apply_aliases(properties, _ROOT_ALIASES)
        preview_order = (
            "documentNumber",
            "status",
            "businessPartnerId",
            "documentAddressName",
            "customerNumber",
            "customerOrderNumber",
            "salesOrderDocumentNumber",
            "documentDate",
            "isDocumentSent",
            "shippingMethodName",
            "salesOrderId",
            "country",
        )
        for idx, key in enumerate(preview_order):
            if key in properties:
                properties[key]["previewable"] = True
                properties[key]["previewOrder"] = idx
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{documentNumber}}",
            "sections": {
                "general": {"label": "General"},
                "address": {"label": "Shipping address"},
                "references": {"label": "References"},
                "shipping": {"label": "Shipping"},
                "content": {"label": "Content"},
                "lineItems": {"label": "Line items"},
                "customFields": {"label": "Custom Fields"},
            },
            "rootNode": {"properties": properties},
            "actions": [
                {
                    "key": "sendToRecipient",
                    "label": "Send to recipient",
                    "bulk": False,
                    "method": "PATCH",
                    "path": "/api/entity/DeliveryNote/actions/sendToRecipient",
                    "destructive": True,
                    "description": "Send this delivery note to its recipient using the document's Xentral send workflow.",
                    "command": self._send_to_recipient_command_schema(),
                },
                *build_document_actions(self),
            ],
            "processSteps": build_process_steps(self),
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    @staticmethod
    def _send_to_recipient_command_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recipientEmail": {"type": "string", "label": "Recipient email"},
                "recipientName": {"type": "string", "label": "Recipient name"},
                "subject": {"type": "string", "label": "Subject"},
                "body": {"type": "string", "label": "Body"},
                "cc": {"type": "string", "label": "CC"},
                "bcc": {"type": "string", "label": "BCC"},
                "printerId": {"type": "string", "label": "Printer ID"},
                "printerName": {"type": "string", "label": "Printer name"},
                "markAsSent": {"type": "boolean", "label": "Mark document as sent"},
            },
        }

    @staticmethod
    def _action_payload(command: dict[str, Any]) -> dict[str, Any]:
        email: dict[str, Any] = {}
        email_map = {
            "recipientEmail": "to",
            "recipientName": "name",
            "subject": "subject",
            "body": "body",
        }
        for source, target in email_map.items():
            value = command.get(source)
            if value not in (None, ""):
                email[target] = deepcopy(value)
        for key in ("cc", "bcc"):
            value = command.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, str):
                email[key] = [item.strip() for item in value.split(",") if item.strip()]
            else:
                email[key] = deepcopy(value)
        if email:
            return {"email": email}

        printer: dict[str, Any] = {}
        for source, target in (("printerId", "id"), ("printerName", "name")):
            value = command.get(source)
            if value not in (None, ""):
                printer[target] = deepcopy(value)
        if printer:
            return {"printer": printer}

        if command.get("markAsSent") is True:
            return {"markAsSent": True}
        return {}

    @staticmethod
    def _postal_address_from_v3(
        value: dict[str, Any] | None, vat_id: Any = None
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict) and vat_id in (None, ""):
            return None
        source = value if isinstance(value, dict) else {}
        address = {
            "name": source.get("name"),
            "department": source.get("department"),
            "subDepartment": source.get("subDepartment"),
            "street": source.get("street"),
            "additionalAddressInformation": source.get("addressSupplement"),
            "contactPerson": source.get("contactPerson"),
            "postalCode": source.get("zipCode"),
            "city": source.get("city"),
            "country": source.get("country"),
            "vatId": vat_id,
        }
        return address

    @staticmethod
    def _postal_address_to_v3(
        value: Any,
    ) -> tuple[dict[str, Any] | None, Any]:
        if value is None:
            return None, None
        if not isinstance(value, dict):
            raise ValueError("postalAddress must be an object or null")
        field_map = {
            "additionalAddressInformation": "addressSupplement",
            "postalCode": "zipCode",
        }
        address: dict[str, Any] = {}
        vat_id = value.get("vatId")
        for key, field_value in value.items():
            if key == "vatId":
                continue
            address[field_map.get(key, key)] = field_value
        return address, vat_id

    @staticmethod
    def _line_item_from_v3(raw: dict[str, Any]) -> dict[str, Any]:
        item = deepcopy(raw)
        if item.get("id") is not None:
            item["id"] = str(item["id"])
            item["uuid"] = item["id"]
        if "order" in item:
            item["sort"] = item.pop("order")
        product = item.get("product")
        if isinstance(product, dict) and product.get("id") is not None:
            product["id"] = str(product["id"])
        item = _apply_aliases(item, _LINE_ITEM_ALIASES)
        for legacy_key, property_key in _LEGACY_LINE_ITEM_FIELD_MAP.items():
            if legacy_key in raw and property_key not in item:
                item[property_key] = raw[legacy_key]
        allowed = set(_apply_aliases(_line_item_properties(), _LINE_ITEM_ALIASES))
        allowed.update(_CSV_LINE_ITEM_PROPERTIES)
        return {key: value for key, value in item.items() if key in allowed}

    @staticmethod
    def _line_item_payload(item: Any, *, creating: bool) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Each lineItems entry must be an object")
        create_fields = {
            "product",
            "quantity",
            "name",
            "description",
            "countryOfOrigin",
            "hsCode",
            "deliveryDate",
            "deliveryDateAsCalendarWeek",
            "internalComment",
            "customerProductNumber",
            "unit",
            "printSettings",
            "deliveredQuantity",
        }
        update_fields = {
            "name",
            "description",
            "quantity",
            "deliveryDate",
            "deliveryDateAsCalendarWeek",
            "internalComment",
            "sort",
            "unit",
            "printSettings",
            "deliveredQuantity",
        }
        allowed = create_fields if creating else update_fields
        payload = {key: deepcopy(value) for key, value in item.items() if key in allowed}
        inverse_aliases = {new: old for old, new in _LINE_ITEM_ALIASES.items()}
        for external_key, internal_key in inverse_aliases.items():
            if external_key in item and internal_key in allowed and internal_key not in payload:
                payload[internal_key] = deepcopy(item[external_key])
        product = payload.get("product")
        if product not in (None, "") and not isinstance(product, dict):
            payload["product"] = {"id": str(product)}
        return payload

    @classmethod
    def _entity_record(cls, raw: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(raw)
        entity_id = record.get("id")
        record["id"] = str(entity_id) if entity_id is not None else None
        record["uuid"] = record["id"]
        record["documentStatus"] = record.pop("status", None)
        record["isWriteProtected"] = record.pop("writeProtection", False)
        record["associatedAddress"] = record.pop("address", None)
        record["postalAddress"] = cls._postal_address_from_v3(
            record.pop("documentAddress", None),
            record.pop("vatId", None),
        )
        record["costCenterValue"] = record.pop("costCenter", None)
        if isinstance(record.get("lineItems"), list):
            record["lineItems"] = [
                cls._line_item_from_v3(item) if isinstance(item, dict) else item
                for item in record["lineItems"]
            ]
        for legacy_key, property_key in _LEGACY_ROOT_FIELD_MAP.items():
            if legacy_key in raw and property_key not in record:
                record[property_key] = raw[legacy_key]
        record = _apply_aliases(record, _ROOT_ALIASES)
        doc_addr = record.get("documentAddress")
        if isinstance(doc_addr, dict):
            if doc_addr.get("name"):
                record["documentAddressName"] = doc_addr["name"]
            if doc_addr.get("country"):
                record["country"] = doc_addr["country"]
        allowed = set(cls().metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in record.items() if key in allowed}

    @classmethod
    def _v3_payload(cls, body: bytes | None, *, line_items_creating: bool = True) -> dict[str, Any]:
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("DeliveryNote payload must be a JSON object")

        field_map = {
            "associatedAddress": "address",
            "businessPartnerId": "address",
            "documentAddress": "documentAddress",
            "costCenterValue": "costCenter",
            "costCenter": "costCenter",
        }
        read_only = {
            "uuid",
            "id",
            "documentNumber",
            "documentStatus",
            "status",
            "isWriteProtected",
            "isDocumentSent",
            "customerNumber",
            "customerOrderNumber",
            "useAlternativeDocumentTitle",
            "isDeliveryNoteToSupplier",
            "supplierNumber",
            "invoice",
            "shippingMethod",
            "customFields",
            "activity",
            "effectiveAddresses",
            "createdAt",
            "updatedAt",
        }
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key in read_only:
                continue
            if key in {"postalAddress", "documentAddress"}:
                address, vat_id = cls._postal_address_to_v3(value)
                out["documentAddress"] = address
                if vat_id is not None:
                    out["vatId"] = vat_id
                continue
            if key == "lineItems":
                if not isinstance(value, list):
                    raise ValueError("lineItems must be an array")
                if line_items_creating:
                    out["lineItems"] = [
                        cls._line_item_payload(item, creating=True) for item in value
                    ]
                else:
                    if not all(isinstance(item, dict) for item in value):
                        raise ValueError("Each lineItems entry must be an object")
                    # Keep ids until the aggregate update has matched each item
                    # to the dedicated V3 line-item endpoint.
                    out["lineItems"] = deepcopy(value)
                continue
            target = field_map.get(key, key)
            if (
                target
                in {
                    "address",
                    "project",
                    "sales",
                    "salesOrder",
                    "orderPicking",
                    "preferredWarehouse",
                    "commissionConsignmentWarehouse",
                    "editor",
                }
                and value not in (None, "")
                and not isinstance(value, dict)
            ):
                value = {"id": str(value)}
            out[target] = value
        return out

    @staticmethod
    def _query(query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        aliases = {
            "documentStatus": "status",
            "status": "status",
            "isWriteProtected": "writeProtection",
            "associatedAddress": "address.id",
            "businessPartnerId": "address.id",
            "project": "project.id",
            "projectId": "project.id",
            "sales": "sales.id",
            "salesId": "sales.id",
            "salesOrder": "salesOrder.id",
            "salesOrderId": "salesOrder.id",
            "orderPicking": "orderPicking.id",
            "orderPickingId": "orderPicking.id",
            "invoice": "invoice.id",
            "invoiceId": "invoice.id",
            "preferredWarehouse": "preferredWarehouse.id",
            "preferredWarehouseId": "preferredWarehouse.id",
            "shippingMethod": "shippingMethod.id",
            "costCenterValue": "costCenter",
            "costCenter": "costCenter",
        }
        translated: list[tuple[str, str]] = []
        for key, value in query:
            if key.endswith("[key]"):
                value = aliases.get(value, value)
            elif key == "sort":
                prefix = "-" if value.startswith("-") else ""
                sort_key = value[1:] if prefix else value
                value = prefix + aliases.get(sort_key, sort_key)
            translated.append((key, value))
        return translated

    @staticmethod
    def _request_headers(token: str, accept_language: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "xentral-ai-agent",
            "X-Pagination": "table",
        }
        if accept_language:
            headers["Accept-Language"] = accept_language
        return headers

    @classmethod
    async def _sync_line_items(
        cls,
        client: httpx.AsyncClient,
        *,
        url: str,
        desired: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> httpx.Response | None:
        current_response = await client.get(
            url,
            params={"include": "lineItems"},
            headers=headers,
        )
        if current_response.status_code >= 400:
            return current_response
        try:
            current_document = current_response.json().get("data", {})
        except (AttributeError, ValueError):
            return current_response

        existing_items = current_document.get("lineItems", [])
        existing_ids = {
            str(item["id"])
            for item in existing_items
            if isinstance(item, dict) and item.get("id") is not None
        }
        desired_ids = {
            str(item["id"])
            for item in desired
            if isinstance(item, dict) and item.get("id") is not None
        }
        unknown_ids = desired_ids - existing_ids
        if unknown_ids:
            missing = ", ".join(sorted(unknown_ids))
            request = httpx.Request("PATCH", url)
            return httpx.Response(
                400,
                request=request,
                json={
                    "title": "Invalid DeliveryNote line item",
                    "detail": f"Unknown line item id(s): {missing}",
                },
            )

        for line_item_id in existing_ids - desired_ids:
            response = await client.delete(
                f"{url}/lineItems/{line_item_id}",
                headers=headers,
            )
            if response.status_code >= 400:
                return response

        for item in desired:
            line_item_id = item.get("id")
            if line_item_id is None:
                payload = cls._line_item_payload(item, creating=True)
                response = await client.post(
                    f"{url}/lineItems",
                    json=payload,
                    headers=headers,
                )
            else:
                payload = cls._line_item_payload(item, creating=False)
                if not payload:
                    continue
                response = await client.patch(
                    f"{url}/lineItems/{line_item_id}",
                    json=payload,
                    headers=headers,
                )
            if response.status_code >= 400:
                return response
        return None

    async def request(
        self,
        *,
        method: str,
        handle: str | None,
        query: list[tuple[str, str]],
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        method = method.upper()
        path = "/api/v3/deliveryNotes"
        if handle:
            if not handle.isdigit():
                return self._json_response(
                    400,
                    {
                        "title": "Invalid DeliveryNote handle",
                        "detail": "Expected the numeric V3 id.",
                    },
                )
            path = f"{path}/{handle}"

        params = self._query(query)
        if method == "GET":
            include = (
                "lineItems,lineItems.product,project,address,tags,activity,"
                "customFields,lineItems.customFields"
                if handle
                else "project,address,tags,customFields"
            )
            if not any(key == "include" for key, _ in params):
                params.append(("include", include))

        request_body: dict[str, Any] | None = None
        if method in {"POST", "PATCH", "PUT"}:
            try:
                request_body = self._v3_payload(
                    body,
                    line_items_creating=method == "POST",
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return self._json_response(
                    400,
                    {"title": "Invalid DeliveryNote payload", "detail": str(exc)},
                )

        headers = self._request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            url = f"{base_url.rstrip('/')}{path}"
            if (
                method in {"PATCH", "PUT"}
                and handle
                and request_body is not None
                and "lineItems" in request_body
            ):
                desired_line_items = request_body.pop("lineItems")
                if request_body:
                    response = await request_client.request(
                        method,
                        url,
                        params=params,
                        json=request_body,
                        headers=headers,
                    )
                    if response.status_code >= 400:
                        return response
                sync_error = await self._sync_line_items(
                    request_client,
                    url=url,
                    desired=desired_line_items,
                    headers=headers,
                )
                if sync_error is not None:
                    return sync_error
                return await request_client.get(
                    url,
                    params={
                        "include": (
                            "lineItems,lineItems.product,project,address,tags,activity,"
                            "customFields,lineItems.customFields"
                        )
                    },
                    headers=headers,
                )
            return await request_client.request(
                method,
                url,
                params=params,
                json=request_body,
                headers=headers,
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400 or not response.content:
            return AdapterResponse(
                response.status_code,
                response.content,
                self._forward_headers(response),
            )

        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code,
                response.content,
                self._forward_headers(response),
            )

        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data["data"] = [
                self._entity_record(row) if isinstance(row, dict) else row for row in data["data"]
            ]
        elif isinstance(data, dict) and isinstance(data.get("data"), dict):
            data["data"] = self._entity_record(data["data"])
        elif isinstance(data, dict):
            data = self._entity_record(data)

        return self._json_response(response.status_code, data, response.headers)

    @staticmethod
    def _forward_headers(response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key in (
                "content-type",
                "content-disposition",
                "etag",
                "cache-control",
                "x-pagination",
            )
            if (value := response.headers.get(key))
        }

    @classmethod
    def _json_response(
        cls,
        status_code: int,
        data: dict[str, Any],
        source_headers: httpx.Headers | None = None,
    ) -> AdapterResponse:
        headers = {"content-type": "application/json"}
        if source_headers is not None:
            headers.update(
                {
                    key: value
                    for key in ("etag", "cache-control", "x-pagination")
                    if (value := source_headers.get(key))
                }
            )
        return AdapterResponse(
            status_code=status_code,
            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        del handle
        return await execute_document_action(
            self,
            action_key=action_key,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
