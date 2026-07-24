from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

_TIMEOUT_SECONDS = 60.0
_DELIVERY_NOTE_INCLUDE = "lineItems,project,address,tags,activity,customFields"


def _property(type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "label": label, "language": "en", **extra}


def _reference(
    label: str,
    reference: str,
    *,
    render_property: str = "id",
    **extra: Any,
) -> dict[str, Any]:
    return _property(
        "reference",
        label,
        reference=reference,
        renderProperty=render_property,
        **extra,
    )


def _json_response(status_code: int, payload: dict[str, Any]) -> AdapterResponse:
    return AdapterResponse(
        status_code=status_code,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return False


def _string_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    if value in (None, ""):
        return None
    return str(value)


def _reference_value(raw: dict[str, Any], *keys: str) -> dict[str, str] | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, dict) and value.get("id") not in (None, ""):
            return {"id": str(value["id"])}
        if value not in (None, ""):
            return {"id": str(value)}
    return None


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    if isinstance(value, dict):
        return value
    return None


class ShippingOrderAdapter:
    """Business-Entity facade for Xentral's open dispatch center queue."""

    manifest = EmulationManifest(
        key="ShippingOrder",
        label_en="Shipping Order",
        category="Warehousing",
        rollout_batch="shipping-order-dispatch-v1",
        adapter="v3-delivery-note-open-shipment",
        source_apis=(
            "/api/v3/deliveryNotes",
            "/api/v1/deliveries",
            "/api/v1/shipments/{id}",
        ),
        operations=("list", "read"),
    )

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        properties: dict[str, Any] = {
            "uuid": _property("string", "UUID", access="readOnly"),
            "id": _property("integer", "ID", access="readOnly", filterable=True, sortable=True),
            "deliveryNote": _reference(
                "Delivery note",
                "DeliveryNote",
                render_property="documentNumber",
                previewable=True,
                section="references",
            ),
            "deliveryNoteDocumentNumber": _property(
                "string",
                "Delivery note number",
                access="readOnly",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            "salesOrder": _reference(
                "Sales order",
                "SalesOrder",
                render_property="documentNumber",
                section="references",
            ),
            "salesOrderDocumentNumber": _property(
                "string",
                "Sales order number",
                access="readOnly",
                searchable=True,
                section="references",
            ),
            "project": _reference(
                "Project",
                "Project",
                render_property="name",
                section="references",
            ),
            "businessPartnerId": _reference(
                "Business partner",
                "BusinessPartner",
                render_property="name",
                section="recipient",
            ),
            "recipientName": _property(
                "string",
                "Recipient name",
                access="readOnly",
                searchable=True,
                previewable=True,
                section="recipient",
            ),
            "recipientCountry": _property(
                "string",
                "Recipient country",
                access="readOnly",
                section="recipient",
            ),
            "documentDate": _property(
                "date",
                "Delivery note date",
                access="readOnly",
                filterable=True,
                sortable=True,
                section="general",
            ),
            "deliveryDate": _property(
                "date",
                "Delivery date",
                access="readOnly",
                section="general",
            ),
            "shippingMethod": _reference(
                "Shipping method",
                "ShippingMethod",
                render_property="name",
                section="shipping",
            ),
            "shippingMethodName": _property(
                "string",
                "Shipping method name",
                access="readOnly",
                searchable=True,
                section="shipping",
            ),
            "trackingNumber": _property(
                "string",
                "Tracking number",
                access="readOnly",
                searchable=True,
                section="shipping",
            ),
            "trackingLink": _property(
                "string", "Tracking link", access="readOnly", section="shipping"
            ),
            "carrier": _property("string", "Carrier", access="readOnly", section="shipping"),
            "weight": _property("decimal", "Weight", access="readOnly", section="shipping"),
            "parcelCount": _property(
                "integer", "Parcel count", access="readOnly", section="shipping"
            ),
            "isOpen": _property(
                "boolean",
                "Open",
                access="readOnly",
                previewable=True,
                section="processing",
            ),
            "isInProcess": _property(
                "boolean",
                "In process",
                access="readOnly",
                section="processing",
            ),
            "processUser": _reference(
                "Process user",
                "BusinessPartner",
                render_property="name",
                access="readOnly",
                section="processing",
            ),
            "isCompleted": _property(
                "boolean",
                "Completed",
                access="readOnly",
                section="processing",
            ),
            "isCronJob": _property(
                "boolean",
                "Reserved by cron job",
                access="readOnly",
                section="processing",
            ),
            "isFurtherDeliveryNote": _property(
                "boolean",
                "Further delivery note",
                access="readOnly",
                section="processing",
            ),
            "orderPicking": _reference(
                "Order picking",
                "OrderPicking",
                section="processing",
            ),
            "pickStatus": _property(
                "string",
                "Pick status",
                access="readOnly",
                section="processing",
            ),
            "box": _property(
                "string", "Box", access="readOnly", searchable=True, section="processing"
            ),
            "isClarificationCase": _property(
                "boolean",
                "Clarification case",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="clarification",
            ),
            "clarificationReason": _property(
                "string",
                "Clarification reason",
                access="readOnly",
                searchable=True,
                previewable=True,
                section="clarification",
            ),
            "sentAt": _property("date", "Sent at", access="readOnly", section="shipping"),
            "sentAtTimestamp": _property(
                "datetime",
                "Sent at timestamp",
                access="readOnly",
                section="shipping",
            ),
            "createdAt": _property("datetime", "Created at", access="readOnly", sortable=True),
            "updatedAt": _property("datetime", "Updated at", access="readOnly", sortable=True),
        }
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{deliveryNoteDocumentNumber}}",
            "sections": {
                "general": {"label": "General"},
                "references": {"label": "References"},
                "recipient": {"label": "Recipient"},
                "shipping": {"label": "Shipping"},
                "processing": {"label": "Processing"},
                "clarification": {"label": "Clarification"},
            },
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    @staticmethod
    def _query(query: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool | None]:
        aliases = {
            "deliveryNoteDocumentNumber": "documentNumber",
            "project": "project.id",
            "projectId": "project.id",
            "businessPartnerId": "address.id",
        }
        allowed_filters = {
            "id",
            "documentNumber",
            "documentDate",
            "customerNumber",
            "documentAddress.country",
            "project.id",
            "address.id",
        }
        allowed_sorts = {
            "id",
            "documentNumber",
            "documentDate",
            "customerNumber",
            "documentAddress.country",
            "project.id",
            "createdAt",
            "updatedAt",
        }
        translated: list[tuple[str, str]] = []
        clarification_filter: bool | None = None
        clarification_filter_prefix: str | None = None
        skipped_filter_prefixes: set[str] = set()
        for key, value in query:
            if key.endswith("[key]"):
                prefix = key.removesuffix("[key]")
                if value in {"isClarificationCase", "clarificationCase"}:
                    clarification_filter_prefix = prefix
                    continue
                value = aliases.get(value, value)
                if value not in allowed_filters:
                    skipped_filter_prefixes.add(prefix)
                    continue
            elif clarification_filter_prefix and key.startswith(clarification_filter_prefix):
                if key.endswith("[value]"):
                    clarification_filter = _truthy(value)
                    continue
                if key.endswith("[op]"):
                    continue
            elif any(key.startswith(prefix) for prefix in skipped_filter_prefixes):
                continue
            elif key == "sort":
                prefix = "-" if value.startswith("-") else ""
                sort_key = value[1:] if prefix else value
                sort_key = aliases.get(sort_key, sort_key)
                if sort_key not in allowed_sorts:
                    continue
                value = prefix + sort_key
            translated.append((key, value))

        return translated, clarification_filter

    @classmethod
    def _entity_record(cls, raw: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(raw)
        shipment = _first_dict(record.get("shipment")) or _first_dict(record.get("shipments")) or {}
        tracking = record.get("tracking") if isinstance(record.get("tracking"), dict) else {}
        delivery_note_id = _string_id(
            record.get("deliveryNote")
            or record.get("deliveryNoteId")
            or record.get("lieferschein")
            or record.get("id")
        )
        shipment_id = _string_id(
            record.get("shipmentId")
            or record.get("dispatchId")
            or record.get("versand")
            or shipment.get("id")
            or record.get("shippingOrderId")
            or delivery_note_id
        )
        completed = _truthy(
            record.get("isCompleted") or record.get("completed") or record.get("abgeschlossen")
        )
        cronjob = _truthy(record.get("isCronJob") or record.get("cronjob"))
        further_delivery_note = _truthy(
            record.get("isFurtherDeliveryNote") or record.get("weitererlieferschein")
        )
        tracking_number = (
            record.get("trackingNumber")
            or record.get("tracking_number")
            or record.get("tracking")
            or tracking.get("number")
            or shipment.get("trackingNumber")
        )
        if isinstance(tracking_number, dict):
            tracking_number = tracking_number.get("number")
        clarification_marker = (
            record.get("isClarificationCase")
            or record.get("clarificationCase")
            or record.get("isInNeedOfClarification")
            or record.get("adressvalidation") == 2
            or shipment.get("adressvalidation") == 2
        )
        clarification_reason = (
            record.get("clarificationReason")
            or record.get("klaergrund")
            or shipment.get("clarificationReason")
            or shipment.get("klaergrund")
        )
        recipient = (
            record.get("documentAddress") if isinstance(record.get("documentAddress"), dict) else {}
        )
        sales_order = record.get("salesOrder")
        shipping_method = record.get("shippingMethod")
        order_picking = record.get("orderPicking") or record.get("kommissionierung")
        out = {
            "id": shipment_id,
            "uuid": shipment_id,
            "deliveryNote": {"id": delivery_note_id} if delivery_note_id else None,
            "deliveryNoteDocumentNumber": record.get("documentNumber")
            or record.get("deliveryNoteDocumentNumber")
            or record.get("belegnr"),
            "salesOrder": _reference_value(
                record, "salesOrder", "salesOrderId", "auftragid", "auftrag"
            ),
            "salesOrderDocumentNumber": (
                record.get("salesOrderDocumentNumber")
                or (sales_order.get("documentNumber") if isinstance(sales_order, dict) else None)
                or record.get("salesOrderNumber")
            ),
            "project": _reference_value(record, "project", "projectId", "projekt"),
            "businessPartnerId": _reference_value(
                record, "address", "businessPartner", "addressId", "adresse"
            ),
            "recipientName": record.get("recipientName")
            or recipient.get("name")
            or record.get("name"),
            "recipientCountry": record.get("recipientCountry")
            or recipient.get("country")
            or record.get("country")
            or record.get("land"),
            "documentDate": record.get("documentDate") or record.get("datum"),
            "deliveryDate": record.get("deliveryDate") or record.get("lieferdatum"),
            "shippingMethod": _reference_value(
                record, "shippingMethod", "shippingMethodId", "versandart"
            ),
            "shippingMethodName": (
                record.get("shippingMethodName")
                or (shipping_method.get("name") if isinstance(shipping_method, dict) else None)
                or record.get("versandart")
            ),
            "trackingNumber": tracking_number,
            "trackingLink": record.get("trackingLink")
            or tracking.get("link")
            or shipment.get("trackingLink"),
            "carrier": record.get("carrier") or tracking.get("carrier") or shipment.get("carrier"),
            "weight": record.get("weight") or record.get("gewicht") or shipment.get("weight"),
            "parcelCount": record.get("parcelCount")
            or record.get("amount")
            or record.get("anzahlpakete"),
            "isOpen": not completed
            and not cronjob
            and (bool(tracking_number) or not further_delivery_note),
            "isInProcess": _truthy(record.get("isInProcess") or record.get("improzess"))
            or _truthy(record.get("versandzweigeteilt")),
            "processUser": _reference_value(
                record, "processUser", "processUserId", "improzessuser"
            ),
            "isCompleted": completed,
            "isCronJob": cronjob,
            "isFurtherDeliveryNote": further_delivery_note,
            "orderPicking": {"id": str(order_picking)}
            if order_picking not in (None, "", {}) and not isinstance(order_picking, dict)
            else order_picking,
            "pickStatus": record.get("pickStatus")
            or record.get("mobilePickingStatus")
            or record.get("mobile_picking_status"),
            "box": record.get("box") or record.get("kiste"),
            "isClarificationCase": _truthy(clarification_marker),
            "clarificationReason": clarification_reason,
            "sentAt": record.get("sentAt") or record.get("versendet_am"),
            "sentAtTimestamp": record.get("sentAtTimestamp")
            or record.get("versendet_am_zeitstempel"),
            "createdAt": record.get("createdAt"),
            "updatedAt": record.get("updatedAt"),
        }
        allowed = set(cls().metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in out.items() if key in allowed and value is not None}

    @classmethod
    def _filter_response(
        cls, response: httpx.Response, clarification_filter: bool | None
    ) -> AdapterResponse:
        if response.status_code >= 400 or clarification_filter is None:
            return AdapterResponse(
                status_code=response.status_code,
                content=response.content,
                headers=dict(response.headers),
            )
        try:
            payload = response.json()
        except ValueError:
            return AdapterResponse(
                status_code=response.status_code,
                content=response.content,
                headers=dict(response.headers),
            )
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            rows = [
                item
                for item in payload["data"]
                if isinstance(item, dict)
                and cls._entity_record(item).get("isClarificationCase") is clarification_filter
            ]
            payload["data"] = rows
            extra = payload.get("extra")
            if isinstance(extra, dict):
                extra["total"] = len(rows)
        return _json_response(response.status_code, payload)

    @classmethod
    def _wrap_response(
        cls,
        response: httpx.Response,
        *,
        list_response: bool = False,
        clarification_filter: bool | None = None,
    ) -> AdapterResponse:
        filtered = cls._filter_response(response, clarification_filter)
        if filtered is not None and filtered.content != response.content:
            try:
                payload = json.loads(filtered.content.decode("utf-8"))
            except ValueError:
                return filtered
        else:
            if response.status_code >= 400:
                return AdapterResponse(
                    response.status_code, response.content, dict(response.headers)
                )
            try:
                payload = response.json()
            except ValueError:
                return AdapterResponse(
                    response.status_code, response.content, dict(response.headers)
                )
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload["data"] = [
                cls._entity_record(item) if isinstance(item, dict) else item
                for item in payload["data"]
            ]
        elif isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload["data"] = cls._entity_record(payload["data"])
        elif isinstance(payload, dict) and not list_response:
            payload = {"data": cls._entity_record(payload)}
        return _json_response(response.status_code, payload)

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
        client: Any | None = None,
    ) -> AdapterResponse:
        if method.upper() != "GET":
            return _json_response(405, {"message": "ShippingOrder is read-only."})

        headers = _request_headers(token, accept_language)
        owns_client = client is None
        http_client = client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
        try:
            if handle:
                shipment_response = await http_client.get(
                    f"{base_url.rstrip('/')}/api/v1/shipments/{handle}",
                    headers=headers,
                )
                if shipment_response.status_code < 400:
                    return self._wrap_response(shipment_response)
                response = await http_client.get(
                    f"{base_url.rstrip('/')}/api/v3/deliveryNotes/{handle}",
                    params={
                        "include": _DELIVERY_NOTE_INCLUDE,
                    },
                    headers=headers,
                )
                return self._wrap_response(response)

            translated_query, clarification_filter = self._query(query)
            response = await http_client.get(
                f"{base_url.rstrip('/')}/api/v3/deliveryNotes",
                params=[
                    ("include", _DELIVERY_NOTE_INCLUDE),
                    *translated_query,
                ],
                headers=headers,
            )
            return self._wrap_response(
                response,
                list_response=True,
                clarification_filter=clarification_filter,
            )
        finally:
            if owns_client:
                await http_client.aclose()

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: Any | None = None,
    ) -> AdapterResponse:
        del action_key, handle, body, base_url, token, accept_language, client
        return _json_response(404, {"message": "ShippingOrder does not expose actions yet."})
