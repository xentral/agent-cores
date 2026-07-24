from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx

from ._search import extract_search, fan_out_search
from entity_registry.core_sdk import AdapterResponse, EmulationManifest

_TIMEOUT_SECONDS = 60.0


def _property(type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "label": label, "language": "en", **extra}


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


def _ref(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        out = deepcopy(value)
        if out.get("id") is not None:
            out["id"] = str(out["id"])
        return out
    return {"id": str(value)}


def _serial_number_properties() -> dict[str, Any]:
    return {"number": _property("string", "Number")}


def _quality_control_attribute_properties() -> dict[str, Any]:
    return {
        "quantity": _property("decimal", "Quantity", access="readOnly"),
        "batch": _property("string", "Batch", filterable=True, searchable=True),
        "bestBeforeDate": _property("date", "Best before date", filterable=True, sortable=True),
        "serialNumbers": _property(
            "collection",
            "Serial numbers",
            node={"properties": _serial_number_properties()},
        ),
    }


def _storage_item_properties() -> dict[str, Any]:
    return {
        "productId": _property(
            "reference", "Product", reference="Product", renderProperty="number", filterable=True
        ),
        "sku": _property("string", "SKU", filterable=True, searchable=True, previewable=True),
        "quantity": _property("decimal", "Quantity", previewable=True),
        "qualityControlAttributes": _property(
            "collection",
            "Quality control attributes",
            node={"properties": _quality_control_attribute_properties()},
        ),
    }


class StorageLocationAdapter:
    manifest = EmulationManifest(
        key="StorageLocation",
        label_en="Storage Location",
        category="MasterData",
        rollout_batch="storage-location-v1",
        adapter="v1-storage-location",
        source_apis=(
            "/api/v1/storageLocations",
            "/api/v1/warehouses/{warehouseId}/storageLocations",
            "/api/v2/warehouses/{warehouseId}/storageLocations/{storageLocationId}/items",
            "/api/v1/warehouses/{warehouseId}/storageLocations/{storageLocationId}/items",
            "/api/v1/storageLocations/setTotalStock",
        ),
        operations=("list", "read", "create", "update", "delete"),
    )

    global_path = "/api/v1/storageLocations"
    payload_read_only_fields = {
        "id",
        "uuid",
        "warehouseId",
        "items",
        "stockDetails",
        "createdAt",
        "updatedAt",
    }
    query_aliases = {
        "id": "id",
        "designation": "designation",
        "warehouse": "warehouse.id",
        "warehouseId": "warehouse.id",
    }
    # The bin label is the only free-text field to search a storage location by.
    # Emulated as a `search` fan-out for parity with the other core entities.
    search_fields = ("designation",)

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        p = _property
        properties: dict[str, Any] = {
            "id": p("integer", "ID", access="readOnly", filterable=True, previewable=True),
            "uuid": p("string", "UUID", access="readOnly"),
            "warehouse": p(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="designation",
                previewable=True,
                section="general",
                rules=["required"],
            ),
            "warehouseId": p(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="designation",
                section="general",
                rules=["required"],
            ),
            "designation": p(
                "string",
                "Designation",
                filterable=True,
                searchable=True,
                previewable=True,
                section="general",
                rules=["required"],
            ),
            "description": p("string", "Description", searchable=True, section="general"),
            "project": p(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                section="references",
            ),
            "projectId": p(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                section="references",
            ),
            "businessPartnerId": p(
                "reference",
                "Business partner",
                reference="BusinessPartner",
                renderProperty="name",
                section="references",
            ),
            "isDeleted": p("boolean", "Deleted", access="readOnly", section="status"),
            "isReplenishmentLocation": p("boolean", "Replenishment location", section="status"),
            "isConsumptionLocation": p("boolean", "Consumption location", section="status"),
            "isRestrictedLocation": p("boolean", "Restricted location", section="status"),
            "hasProductionAccess": p("boolean", "Production access", section="status"),
            "isPosLocation": p("boolean", "POS location", section="status"),
            "dimensions": p(
                "embedded",
                "Dimensions",
                section="dimensions",
                properties={
                    "length": p("decimal", "Length"),
                    "width": p("decimal", "Width"),
                    "height": p("decimal", "Height"),
                },
            ),
            "locationType": p(
                "select",
                "Location type",
                section="dimensions",
                options=[
                    {"value": "pallet", "label": "Pallet"},
                    {"value": "shelf", "label": "Shelf"},
                ],
            ),
            "abcCategory": p(
                "select",
                "ABC category",
                section="dimensions",
                options=[
                    {"value": "A", "label": "A"},
                    {"value": "B", "label": "B"},
                    {"value": "C", "label": "C"},
                ],
            ),
            "sort": p("integer", "Sort", section="dimensions"),
            "items": p(
                "collection",
                "Items",
                access="readOnly",
                section="stock",
                node={"properties": _storage_item_properties()},
            ),
            "stockDetails": p(
                "collection",
                "Stock details",
                access="readOnly",
                section="stock",
                node={"properties": _storage_item_properties()},
            ),
            "stockSummary": p(
                "embedded",
                "Stock summary",
                access="readOnly",
                section="stock",
                properties={
                    "productCount": p("integer", "Product count"),
                    "totalQuantity": p("decimal", "Total quantity"),
                },
            ),
            "createdAt": p("datetime", "Created at", access="readOnly", section="system"),
            "updatedAt": p("datetime", "Updated at", access="readOnly", section="system"),
        }
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "searchFields": list(self.search_fields),
            # Stock movements change the location's stock — state-changing, so
            # they live only in processSteps, never as (state-neutral) actions.
            "processSteps": [
                {
                    "key": "stock",
                    "label": "Stock",
                    "commands": [
                        {
                            "key": "stockItem",
                            "label": "Stock item",
                            "action": "stockItem",
                            "command": self._stock_command_schema(),
                        },
                        {
                            "key": "retrieveItem",
                            "label": "Retrieve item",
                            "action": "retrieveItem",
                            "command": self._stock_command_schema(),
                        },
                        {
                            "key": "setTotalStock",
                            "label": "Set total stock",
                            "action": "setTotalStock",
                            "command": self._set_total_stock_command_schema(),
                        },
                    ],
                }
            ],
            "previewTemplateString": "{{designation}}",
            "sections": {
                "general": {"label": "General"},
                "references": {"label": "References"},
                "status": {"label": "Status"},
                "dimensions": {"label": "Dimensions"},
                "stock": {"label": "Stock"},
                "system": {"label": "System"},
            },
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    @staticmethod
    def _stock_command_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "warehouseId": {"type": "string", "label": "Warehouse ID"},
                "sku": {"type": "string", "label": "SKU"},
                "product": {
                    "type": "object",
                    "label": "Product",
                    "properties": {"sku": {"type": "string", "label": "SKU"}},
                },
                "quantity": {"type": "number", "label": "Quantity"},
                "batch": {"type": "string", "label": "Batch"},
                "bestBeforeDate": {"type": "date", "label": "Best before date"},
                "serialNumbers": {
                    "type": "array",
                    "label": "Serial numbers",
                    "items": {
                        "type": "object",
                        "properties": {"number": {"type": "string", "label": "Number"}},
                    },
                },
                "reason": {"type": "string", "label": "Reason"},
                "project": {
                    "type": "object",
                    "label": "Project",
                    "properties": {"id": {"type": "string", "label": "ID"}},
                },
            },
            # A stock movement is meaningless without WHAT (sku), HOW MUCH
            # (quantity) and INTO WHICH warehouse (warehouseId) — mark all three
            # required so every consumer (Studio action picker, workflow node,
            # MCP) surfaces them as mandatory instead of buried optionals.
            "required": ["sku", "quantity", "warehouseId"],
        }

    @staticmethod
    def _set_total_stock_command_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "warehouseId": {"type": "string", "label": "Warehouse ID"},
                "totalStock": {
                    "type": "array",
                    "label": "Total stock",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product": {
                                "type": "object",
                                "properties": {"id": {"type": "string", "label": "ID"}},
                            },
                            "quantity": {"type": "number", "label": "Quantity"},
                            "qualityControlAttributes": {
                                "type": "object",
                                "properties": {
                                    "batch": {"type": "string", "label": "Batch"},
                                    "bestBeforeDate": {"type": "date", "label": "Best before date"},
                                    "serialNumbers": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {"number": {"type": "string"}},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "required": ["totalStock"],
        }

    @classmethod
    def _query(
        cls, query: list[tuple[str, str]], *, scoped_to_warehouse: bool
    ) -> tuple[list[tuple[str, str]], str | None]:
        translated: list[tuple[str, str]] = []
        warehouse_id: str | None = None
        for key, value in query:
            lookup = key[:-6] if key.endswith("[key]") else key
            if key == "page":
                translated.append(("page[number]", value))
                continue
            if key == "perPage":
                translated.append(("page[size]", value))
                continue
            if key.endswith("[key]"):
                alias = cls.query_aliases.get(value, value)
                if scoped_to_warehouse and alias == "id":
                    alias = "storageLocationId"
                value = alias
            elif lookup in {"warehouseId", "warehouse"}:
                warehouse_id = value
                continue
            elif key == "sort":
                continue
            translated.append((key, value))
        return translated, warehouse_id

    @staticmethod
    def _warehouse_id_from_record(record: dict[str, Any]) -> str | None:
        for key in ("warehouseId", "warehouse"):
            value = record.get(key)
            if isinstance(value, dict) and value.get("id") not in (None, ""):
                return str(value["id"])
            if value not in (None, "") and not isinstance(value, dict):
                return str(value)
        return None

    @classmethod
    def _entity_record(cls, raw: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(raw)
        if record.get("id") is not None:
            record["id"] = str(record["id"])
            record["uuid"] = record.get("uuid") or record["id"]
        warehouse = _ref(record.get("warehouse") or record.get("warehouseId"))
        if warehouse is not None:
            record["warehouse"] = warehouse
            record["warehouseId"] = warehouse
        project = _ref(record.get("project") or record.get("projectId"))
        if project is not None:
            record["project"] = project
            record["projectId"] = project
        partner = _ref(record.get("address") or record.get("businessPartnerId"))
        if partner is not None:
            record["businessPartnerId"] = partner
        if "productionAccess" in record and "hasProductionAccess" not in record:
            record["hasProductionAccess"] = record.pop("productionAccess")
        if "allowsProductionAccess" in record and "hasProductionAccess" not in record:
            record["hasProductionAccess"] = record.pop("allowsProductionAccess")
        items = record.get("items") if isinstance(record.get("items"), list) else []
        if "stockDetails" not in record and items:
            record["stockDetails"] = deepcopy(items)
        cls._apply_stock_summary(record)
        allowed = set(cls().metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in record.items() if key in allowed and value is not None}

    @staticmethod
    def _apply_stock_summary(record: dict[str, Any]) -> None:
        items = record.get("items")
        if not isinstance(items, list):
            return
        total_quantity = 0.0
        for item in items:
            if isinstance(item, dict):
                try:
                    total_quantity += float(item.get("quantity") or 0)
                except (TypeError, ValueError):
                    pass
        record["stockSummary"] = {
            "productCount": len(items),
            "totalQuantity": total_quantity,
        }

    @classmethod
    def _storage_location_payload(cls, body: bytes | None) -> tuple[dict[str, Any], str | None]:
        if not body:
            return {}, None
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("StorageLocation payload must be a JSON object")
        out: dict[str, Any] = {}
        warehouse_id: str | None = None
        for key, value in payload.items():
            if key in {"warehouse", "warehouseId"}:
                if isinstance(value, dict):
                    warehouse_id = (
                        str(value.get("id")) if value.get("id") not in (None, "") else None
                    )
                elif value not in (None, ""):
                    warehouse_id = str(value)
                continue
            if key in cls.payload_read_only_fields:
                continue
            if key in {"project", "projectId", "businessPartnerId"}:
                out["project" if key in {"project", "projectId"} else "address"] = _ref(value)
                continue
            if key == "hasProductionAccess":
                out["allowsProductionAccess"] = value
                continue
            out[key] = deepcopy(value)
        return out, warehouse_id

    @classmethod
    def _items_payload(cls, command: dict[str, Any]) -> dict[str, Any] | AdapterResponse:
        product = command.get("product")
        sku = command.get("sku") or command.get("productSku") or command.get("productNumber")
        if isinstance(product, dict):
            sku = product.get("sku") or product.get("number") or sku
        if not sku:
            return _json_response(
                422, {"message": "command.sku or command.product.sku is required."}
            )
        if command.get("quantity") in (None, ""):
            return _json_response(422, {"message": "command.quantity is required."})
        payload: dict[str, Any] = {
            "product": {"sku": str(sku)},
            "quantity": command["quantity"],
        }
        for key in ("batch", "bestBeforeDate", "serialNumbers", "reason", "project"):
            value = command.get(key)
            if value not in (None, ""):
                payload[key] = deepcopy(value)
        if "bestBefore" in command and "bestBeforeDate" not in payload:
            payload["bestBeforeDate"] = command["bestBefore"]
        return payload

    @staticmethod
    def _set_total_stock_payload(
        storage_location_id: str, command: dict[str, Any]
    ) -> dict[str, Any] | AdapterResponse:
        total_stock = command.get("totalStock")
        if not isinstance(total_stock, list):
            return _json_response(422, {"message": "command.totalStock must be an array."})
        normalized: list[dict[str, Any]] = []
        for item in total_stock:
            if not isinstance(item, dict):
                return _json_response(422, {"message": "Each totalStock item must be an object."})
            product = item.get("product")
            product_id = product.get("id") if isinstance(product, dict) else item.get("productId")
            if product_id in (None, ""):
                return _json_response(
                    422, {"message": "Each totalStock item requires product.id or productId."}
                )
            stock_item: dict[str, Any] = {
                "product": {"id": str(product_id)},
                "quantity": item.get("quantity", 0),
            }
            qca = item.get("qualityControlAttributes") or {}
            if isinstance(qca, dict) and qca:
                qca = deepcopy(qca)
                if "bestBefore" in qca and "bestBeforeDate" not in qca:
                    qca["bestBeforeDate"] = qca.pop("bestBefore")
                stock_item["qualityControlAttributes"] = qca
            normalized.append(stock_item)
        return {
            "data": [
                {
                    "storageLocation": {"id": storage_location_id},
                    "totalStock": normalized,
                }
            ]
        }

    @staticmethod
    def _forward_headers(response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key in ("content-type", "etag", "cache-control", "x-pagination")
            if (value := response.headers.get(key))
        }

    @classmethod
    def _wrap_payload(cls, response: httpx.Response) -> AdapterResponse:
        if response.status_code >= 400 or not response.content:
            return AdapterResponse(
                response.status_code, response.content, cls._forward_headers(response)
            )
        try:
            payload = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code, response.content, cls._forward_headers(response)
            )
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload["data"] = [
                cls._entity_record(item) if isinstance(item, dict) else item
                for item in payload["data"]
            ]
        elif isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload["data"] = cls._entity_record(payload["data"])
        elif isinstance(payload, dict):
            payload = {"data": cls._entity_record(payload)}
        return _json_response(response.status_code, payload)

    async def _resolve_location(
        self,
        request_client: httpx.AsyncClient,
        *,
        base_url: str,
        storage_location_id: str,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        # There is NO single-record read route for storage locations — both
        # /api/v1/storageLocations/{id} and the warehouse-scoped variant return
        # "Route not found". So resolve by filtering the list by id. Two v1
        # quirks make this fragile if you get them wrong (both confirmed live):
        #   • ``filter[id][equals]`` DOES work and returns the exact row.
        #   • but this endpoint only accepts a SMALL page size — 10 is fine,
        #     1 (the old value) and 100/250 all 400. Use 10.
        response = await request_client.get(
            f"{base_url.rstrip('/')}{self.global_path}",
            params={
                "filter[0][key]": "id",
                "filter[0][op]": "equals",
                "filter[0][value]": str(storage_location_id),
                "page[number]": "1",
                "page[size]": "10",
            },
            headers=headers,
        )
        if response.status_code >= 400:
            return None
        try:
            rows = response.json().get("data", [])
        except (AttributeError, ValueError):
            return None
        return rows[0] if rows and isinstance(rows[0], dict) else None

    async def _fetch_items(
        self,
        request_client: httpx.AsyncClient,
        *,
        base_url: str,
        storage_location_id: str,
        warehouse_id: str,
        headers: dict[str, str],
    ) -> list[Any]:
        response = await request_client.get(
            f"{base_url.rstrip('/')}/api/v2/warehouses/{warehouse_id}/storageLocations/{storage_location_id}/items",
            headers=headers,
        )
        if response.status_code >= 400:
            return []
        try:
            data = response.json().get("data", [])
        except (AttributeError, ValueError):
            return []
        return data if isinstance(data, list) else []

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
        if method == "GET" and not handle:
            search = extract_search(query)
            if search is not None and search[0]:
                return await fan_out_search(
                    self,
                    query=query,
                    value=search[0],
                    op=search[1],
                    search_fields=self.search_fields,
                    base_url=base_url,
                    token=token,
                    accept_language=accept_language,
                    client=client,
                )
        if handle and not str(handle).isdigit():
            return _json_response(
                400,
                {
                    "title": "Invalid StorageLocation handle",
                    "detail": "Expected the numeric storage location id.",
                },
            )

        request_body: dict[str, Any] | None = None
        body_warehouse_id: str | None = None
        if method in {"POST", "PATCH", "PUT"}:
            try:
                request_body, body_warehouse_id = self._storage_location_payload(body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return _json_response(
                    400, {"title": "Invalid StorageLocation payload", "detail": str(exc)}
                )

        headers = _request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> AdapterResponse:
            base = base_url.rstrip("/")
            scoped_params, query_warehouse_id = self._query(query, scoped_to_warehouse=True)
            global_params, _ = self._query(query, scoped_to_warehouse=False)
            warehouse_id = body_warehouse_id or query_warehouse_id

            if method == "GET" and handle:
                location = await self._resolve_location(
                    request_client,
                    base_url=base_url,
                    storage_location_id=str(handle),
                    headers=headers,
                )
                if not location:
                    return _json_response(404, {"message": "Storage location not found."})
                resolved_warehouse_id = warehouse_id or self._warehouse_id_from_record(location)
                if resolved_warehouse_id:
                    location["items"] = await self._fetch_items(
                        request_client,
                        base_url=base_url,
                        storage_location_id=str(handle),
                        warehouse_id=resolved_warehouse_id,
                        headers=headers,
                    )
                return _json_response(200, {"data": self._entity_record(location)})

            if method == "GET":
                if warehouse_id:
                    response = await request_client.get(
                        f"{base}/api/v1/warehouses/{warehouse_id}/storageLocations",
                        params=scoped_params,
                        headers=headers,
                    )
                else:
                    response = await request_client.get(
                        f"{base}{self.global_path}",
                        params=global_params,
                        headers=headers,
                    )
                return self._wrap_payload(response)

            if method == "POST":
                if not warehouse_id:
                    return _json_response(
                        422,
                        {
                            "message": "warehouseId or warehouse.id is required to create a storage location."
                        },
                    )
                response = await request_client.post(
                    f"{base}/api/v1/warehouses/{warehouse_id}/storageLocations",
                    json=request_body,
                    headers=headers,
                )
                return self._wrap_payload(response)

            if method in {"PATCH", "PUT", "DELETE"}:
                if not handle:
                    return _json_response(422, {"message": "StorageLocation id is required."})
                if not warehouse_id:
                    location = await self._resolve_location(
                        request_client,
                        base_url=base_url,
                        storage_location_id=str(handle),
                        headers=headers,
                    )
                    warehouse_id = self._warehouse_id_from_record(location or {})
                if not warehouse_id:
                    return _json_response(
                        422,
                        {
                            "message": "warehouseId or warehouse.id is required for this storage location operation."
                        },
                    )
                response = await request_client.request(
                    "PATCH" if method == "PUT" else method,
                    f"{base}/api/v1/warehouses/{warehouse_id}/storageLocations/{handle}",
                    json=request_body if method != "DELETE" else None,
                    headers=headers,
                )
                if method == "DELETE" and (response.status_code == 204 or not response.content):
                    return _json_response(
                        200,
                        {
                            "deleted": True,
                            "storageLocationId": str(handle),
                            "warehouseId": warehouse_id,
                        },
                    )
                return self._wrap_payload(response)

            return _json_response(405, {"message": f"Unsupported StorageLocation method: {method}"})

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                return await _perform(request_client)
        return await _perform(client)

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
        if action_key not in {"stockItem", "retrieveItem", "setTotalStock"}:
            return _json_response(404, {"message": f"Unknown StorageLocation action: {action_key}"})
        try:
            envelope = json.loads((body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _json_response(422, {"message": f"Invalid action envelope: {exc}"})
        if not isinstance(envelope, dict):
            return _json_response(422, {"message": "Action envelope must be a JSON object."})
        ids = envelope.get("ids") or ([handle] if handle else None)
        if not isinstance(ids, list) or not ids:
            return _json_response(422, {"message": "ids must be a non-empty array."})
        if len(ids) != 1:
            return _json_response(
                422, {"message": "StorageLocation actions only support one storage location id."}
            )
        storage_location_id = str(ids[0]).strip()
        if not storage_location_id.isdigit():
            return _json_response(422, {"message": "StorageLocation id must be numeric."})
        command = envelope.get("command") or {}
        if not isinstance(command, dict):
            return _json_response(422, {"message": "command must be an object."})

        warehouse_id = command.get("warehouseId")
        if isinstance(command.get("warehouse"), dict):
            warehouse_id = command["warehouse"].get("id") or warehouse_id
        warehouse_id = str(warehouse_id) if warehouse_id not in (None, "") else None

        headers = _request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> AdapterResponse:
            nonlocal warehouse_id
            base = base_url.rstrip("/")
            if not warehouse_id and action_key != "setTotalStock":
                location = await self._resolve_location(
                    request_client,
                    base_url=base_url,
                    storage_location_id=storage_location_id,
                    headers=headers,
                )
                warehouse_id = self._warehouse_id_from_record(location or {})
            if action_key in {"stockItem", "retrieveItem"}:
                if not warehouse_id:
                    return _json_response(
                        422,
                        {
                            "message": "command.warehouseId or a resolvable storage location warehouse is required."
                        },
                    )
                payload = self._items_payload(command)
                if isinstance(payload, AdapterResponse):
                    return payload
                method = "POST" if action_key == "stockItem" else "PATCH"
                response = await request_client.request(
                    method,
                    f"{base}/api/v1/warehouses/{warehouse_id}/storageLocations/{storage_location_id}/items",
                    json=payload,
                    headers=headers,
                )
                if response.status_code >= 400:
                    return AdapterResponse(
                        response.status_code, response.content, self._forward_headers(response)
                    )
                return _json_response(
                    200,
                    {
                        "message": "Stock item booked."
                        if action_key == "stockItem"
                        else "Stock item retrieved.",
                        "data": {
                            "storageLocation": {"id": storage_location_id},
                            "warehouse": {"id": warehouse_id},
                            "payload": payload,
                        },
                    },
                )

            payload = self._set_total_stock_payload(storage_location_id, command)
            if isinstance(payload, AdapterResponse):
                return payload
            response = await request_client.patch(
                f"{base}/api/v1/storageLocations/setTotalStock",
                json=payload,
                headers=headers,
            )
            if response.status_code >= 400:
                return AdapterResponse(
                    response.status_code, response.content, self._forward_headers(response)
                )
            return _json_response(
                200,
                {
                    "message": "Total stock set.",
                    "data": {"storageLocation": {"id": storage_location_id}, "payload": payload},
                },
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                return await _perform(request_client)
        return await _perform(client)
