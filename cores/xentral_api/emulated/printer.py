from __future__ import annotations

import json
import asyncio
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

_TIMEOUT_SECONDS = 60.0


def _property(type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "label": label, **extra}


def _json_response(status_code: int, payload: dict[str, Any]) -> AdapterResponse:
    return AdapterResponse(
        status_code,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"content-type": "application/json"},
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
        child = deepcopy(value)
        if child.get("id") is not None:
            child["id"] = str(child["id"])
        return child
    return {"id": str(value)}


def _printer_setup_properties() -> dict[str, Any]:
    return {
        "id": _property("string", "ID", access="readOnly"),
        "name": _property("string", "Name", filterable=True, searchable=True, previewable=True),
        "connectionType": _property(
            "select", "Connection type", options=_connection_type_options()
        ),
        "description": _property("string", "Description"),
        "config": _property("string", "Config"),
        "createdAt": _property("datetime", "Created at", access="readOnly"),
    }


def _print_job_properties() -> dict[str, Any]:
    return {
        "id": _property("string", "ID", access="readOnly"),
        "fileName": _property(
            "string", "File name", filterable=True, searchable=True, previewable=True
        ),
        "printerId": _property(
            "reference", "Printer", reference="Printer", renderProperty="name", filterable=True
        ),
        "deviceGatewayId": _property(
            "reference",
            "Device gateway",
            reference="DeviceGateway",
            renderProperty="uuid",
            filterable=True,
        ),
        "job": _property("string", "Job", access="readOnly"),
        "status": _property(
            "select",
            "Status",
            filterable=True,
            previewable=True,
            options=_print_job_status_options(),
        ),
        "reason": _property("string", "Reason"),
        "createdAt": _property("datetime", "Created at", access="readOnly"),
        "updatedAt": _property("datetime", "Updated at", access="readOnly"),
    }


def _connection_type_options() -> list[dict[str, str]]:
    return [
        {"value": "cups", "label": "CUPS"},
        {"value": "pdf", "label": "PDF"},
        {"value": "download", "label": "Download"},
        {"value": "nextgen_spooler", "label": "NextGen spooler"},
        {"value": "spooler", "label": "Spooler"},
        {"value": "adapterbox", "label": "Adapterbox"},
    ]


def _printer_type_options() -> list[dict[str, str]]:
    return [
        {"value": "general", "label": "General"},
        {"value": "fax", "label": "Fax"},
        {"value": "label", "label": "Label"},
    ]


def _printer_use_case_options() -> list[dict[str, str]]:
    return [
        {"value": "shipping-labels", "label": "Shipping labels"},
        {"value": "shipping-documents", "label": "Shipping documents"},
        {"value": "other", "label": "Other"},
    ]


def _printer_print_method_options() -> list[dict[str, str]]:
    return [
        {"value": "shipping-method", "label": "Shipping method"},
        {"value": "project", "label": "Project"},
        {"value": "user", "label": "User"},
        {"value": "location", "label": "Location"},
        {"value": "later", "label": "Later"},
    ]


def _print_job_status_options() -> list[dict[str, str]]:
    return [
        {"value": "created", "label": "Created"},
        {"value": "pending", "label": "Pending"},
        {"value": "processing", "label": "Processing"},
        {"value": "completed", "label": "Completed"},
        {"value": "failed", "label": "Failed"},
    ]


class PrinterAdapter:
    manifest = EmulationManifest(
        key="Printer",
        label_en="Printer",
        category="Configuration",
        rollout_batch="printer-configuration-v1",
        adapter="v1-printer-print-jobs",
        source_apis=("/api/v1/printers", "/api/v1/printJobs"),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v1/printers"
    payload_read_only_fields = {
        "id",
        "createdAt",
        "updatedAt",
        "printerSetups",
        "printJobs",
    }
    filter_aliases = {
        "id": "id",
        "name": "name",
        "designation": "designation",
        "active": "active",
        "isActive": "active",
        "isDraft": "isDraft",
        "connection": "connection",
        "connectionType": "connection",
        "spooler": "spooler",
        "deviceId": "deviceId",
        "search": "search",
    }
    supported_filter_fields = set(filter_aliases)
    query_aliases = {
        "id": "id",
        "name": "name",
        "designation": "designation",
        "active": "active",
        "isActive": "active",
        "isDraft": "isDraft",
        "connection": "connection",
        "connectionType": "connection",
        "spooler": "spooler",
        "deviceId": "deviceId",
        "search": "search",
    }

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        p = _property
        properties: dict[str, Any] = {
            "id": p("integer", "ID", access="readOnly"),
            "name": p(
                "string",
                "Name",
                filterable=True,
                searchable=True,
                previewable=True,
                section="general",
                rules=["required"],
            ),
            "designation": p(
                "string", "Designation", filterable=True, searchable=True, section="general"
            ),
            "description": p(
                "string", "Description", searchable=True, previewable=True, section="general"
            ),
            "command": p("string", "Command", section="connection"),
            "active": p("boolean", "Active", filterable=True, previewable=True, section="general"),
            "isActive": p("boolean", "Active", previewable=True, section="general"),
            "isDraft": p("boolean", "Draft", filterable=True, section="general"),
            "toMailAddress": p("string", "To mail address", section="email"),
            "toMailText": p("string", "To mail text", section="email"),
            "toMailSubject": p("string", "To mail subject", section="email"),
            "adapterBoxIp": p("string", "Adapterbox IP", section="connection"),
            "adapterBoxSerialNumber": p("string", "Adapterbox serial number", section="connection"),
            "adapterBoxPassword": p(
                "string", "Adapterbox password", access="writeOnly", section="connection"
            ),
            "connection": p(
                "select",
                "Connection",
                filterable=True,
                section="connection",
                options=_connection_type_options(),
            ),
            "connectionType": p(
                "select",
                "Connection type",
                section="connection",
                options=_connection_type_options(),
            ),
            "spooler": p("string", "Spooler", filterable=True, section="connection"),
            "deviceId": p("string", "Device ID", filterable=True, section="connection"),
            "type": p("select", "Printer type", section="general", options=_printer_type_options()),
            "format": p("string", "Format", section="general"),
            "hasNoBackground": p("boolean", "No background", section="general"),
            "configJson": p("string", "Config JSON", section="connection"),
            "useCase": p(
                "select", "Use case", section="routing", options=_printer_use_case_options()
            ),
            "printMethod": p(
                "select", "Print method", section="routing", options=_printer_print_method_options()
            ),
            "printerSetups": p(
                "collection",
                "Printer setups",
                access="readOnly",
                section="connection",
                node={"properties": _printer_setup_properties()},
            ),
            "printJobs": p(
                "collection",
                "Print jobs",
                access="readOnly",
                section="jobs",
                node={"properties": _print_job_properties()},
            ),
            "createdAt": p("datetime", "Created at", access="readOnly", section="system"),
            "updatedAt": p("datetime", "Updated at", access="readOnly", section="system"),
        }
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "actions": [
                {
                    "key": "printDocument",
                    "label": "Print document",
                    "bulk": False,
                    "method": "PATCH",
                    "path": "/api/entity/Printer/actions/printDocument",
                    "destructive": True,
                    "description": "Create a print job for a base64-encoded PDF on the selected printer.",
                    "command": {
                        "type": "object",
                        "properties": {
                            "fileContent": {
                                "type": "string",
                                "label": "PDF base64",
                                "description": "Base64-encoded PDF file data.",
                            },
                            "fileName": {
                                "type": "string",
                                "label": "File name",
                            },
                            "quantity": {
                                "type": "integer",
                                "label": "Quantity",
                                "default": 1,
                            },
                        },
                        "required": ["fileContent"],
                    },
                }
            ],
            "previewTemplateString": "{{name}}",
            "sections": {
                "general": {"label": "General"},
                "connection": {"label": "Connection"},
                "email": {"label": "Email"},
                "routing": {"label": "Routing"},
                "jobs": {"label": "Print Jobs"},
                "system": {"label": "System"},
            },
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    @classmethod
    def _query(cls, query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        translated: list[tuple[str, str]] = []
        skipped_filter_indices: set[str] = set()
        for key, value in query:
            # The V1 API paginates with bracketed params
            # (``page[number]`` / ``page[size]`` — the same form this adapter
            # uses for its own subresource calls), but the entity browser and
            # the workflow node send V3-style scalar ``page`` / ``perPage``.
            # Pass those through untranslated and V1 rejects the request (400),
            # so map them to the bracketed form here.
            if key == "page":
                translated.append(("page[number]", value))
                continue
            if key == "perPage":
                translated.append(("page[size]", value))
                continue
            if key == "sort":
                continue
            lookup = key[:-6] if key.endswith("[key]") else key
            if key.endswith("[key]"):
                filter_index = key[:-5]
                if value not in cls.supported_filter_fields:
                    skipped_filter_indices.add(filter_index)
                    continue
                value = cls.filter_aliases.get(value, value)
            elif lookup in cls.query_aliases and value == "":
                value = cls.query_aliases[lookup]
            if any(key.startswith(f"{filter_index}[") for filter_index in skipped_filter_indices):
                continue
            translated.append((key, value))
        return translated

    @classmethod
    def _entity_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(record)
        if record.get("id") is not None:
            record["id"] = str(record["id"])
        if "active" in record and "isActive" not in record:
            record["isActive"] = record["active"]
        if "isActive" in record and "active" not in record:
            record["active"] = record["isActive"]
        if "designation" not in record and record.get("name") is not None:
            record["designation"] = record.get("name")
        if "connection" in record and "connectionType" not in record:
            record["connectionType"] = record["connection"]
        if "connectionType" in record and "connection" not in record:
            record["connection"] = record["connectionType"]
        for key in ("printerSetups", "printJobs"):
            if isinstance(record.get(key), list):
                record[key] = [
                    cls._child_record(item) if isinstance(item, dict) else item
                    for item in record[key]
                ]
        if "printerSetup" in record and "printerSetups" not in record:
            setup = record.pop("printerSetup")
            record["printerSetups"] = [cls._child_record(setup)] if isinstance(setup, dict) else []
        allowed = set(cls().metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in record.items() if key in allowed}

    @staticmethod
    def _child_record(item: dict[str, Any]) -> dict[str, Any]:
        child = deepcopy(item)
        if child.get("id") is not None:
            child["id"] = str(child["id"])
        if "printerId" in child:
            child["printerId"] = _ref(child.get("printerId"))
        if "printer" in child and "printerId" not in child:
            child["printerId"] = _ref(child.get("printer"))
        if "deviceGatewayId" in child:
            child["deviceGatewayId"] = _ref(child.get("deviceGatewayId"))
        return child

    @classmethod
    def _v1_payload(cls, body: bytes | None) -> dict[str, Any]:
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Printer payload must be a JSON object")
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key in cls.payload_read_only_fields:
                continue
            out[key] = value
        return out

    @staticmethod
    def _response_id(response: httpx.Response) -> str | None:
        try:
            data = response.json()
            if isinstance(data, dict):
                candidate = data.get("data", data)
                if isinstance(candidate, dict) and candidate.get("id") is not None:
                    return str(candidate["id"])
        except Exception:
            return None
        location = response.headers.get("location") or response.headers.get("Location")
        if location:
            tail = location.rstrip("/").split("/")[-1]
            if tail:
                return tail
        return None

    async def _fetch_subresources(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        printer_id: str,
        headers: dict[str, str],
        printer: dict[str, Any],
    ) -> None:
        base = base_url.rstrip("/")
        jobs_req = client.get(
            f"{base}/api/v1/printJobs",
            params={
                "filter[0][key]": "printerId",
                "filter[0][op]": "equals",
                "filter[0][value]": printer_id,
                "page[number]": "1",
                "page[size]": "50",
            },
            headers=headers,
        )
        setups_req = client.get(
            f"{base}/api/v1/printerSetups",
            params={
                "filter[0][key]": "printer",
                "filter[0][op]": "equals",
                "filter[0][value]": printer_id,
                "page[number]": "1",
                "page[size]": "50",
            },
            headers=headers,
        )
        jobs_resp, setups_resp = await asyncio.gather(
            jobs_req,
            setups_req,
            return_exceptions=True,
        )
        if not isinstance(jobs_resp, Exception) and jobs_resp.status_code == 200:
            printer["printJobs"] = jobs_resp.json().get("data", [])
        else:
            printer["printJobs"] = []
        if not isinstance(setups_resp, Exception) and setups_resp.status_code == 200:
            printer["printerSetups"] = setups_resp.json().get("data", [])
        else:
            printer["printerSetups"] = []

    async def _find_printer_by_id(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        printer_id: str,
        headers: dict[str, str],
    ) -> tuple[dict[str, Any] | None, httpx.Request | None, httpx.Response | None]:
        base = base_url.rstrip("/")
        last_request: httpx.Request | None = None
        for page_number in range(1, 11):
            response = await client.get(
                f"{base}{self.base_path}",
                params={
                    "page[number]": str(page_number),
                    "page[size]": "50",
                },
                headers=headers,
            )
            last_request = response.request
            if response.status_code >= 400:
                return None, last_request, response
            try:
                rows = response.json().get("data", [])
            except (AttributeError, ValueError):
                return None, last_request, response
            if not isinstance(rows, list) or not rows:
                return None, last_request, response
            for row in rows:
                if isinstance(row, dict) and str(row.get("id")) == str(printer_id):
                    return row, last_request, response
            if len(rows) < 50:
                return None, last_request, response
        return None, last_request, None

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
        path = self.base_path
        if handle:
            if not handle.isdigit():
                return _json_response(
                    400,
                    {"title": "Invalid Printer handle", "detail": "Expected the numeric V1 id."},
                )

        params = self._query(query)
        request_body: dict[str, Any] | None = None
        if method in {"POST", "PATCH", "PUT"}:
            try:
                request_body = self._v1_payload(body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return _json_response(400, {"title": "Invalid Printer payload", "detail": str(exc)})

        headers = _request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            base = base_url.rstrip("/")
            url = f"{base}{path}"
            if method == "GET" and handle:
                printer, request_obj, error_response = await self._find_printer_by_id(
                    request_client,
                    base_url,
                    handle,
                    headers,
                )
                if error_response is not None and error_response.status_code >= 400:
                    return error_response
                if not printer:
                    return httpx.Response(
                        404,
                        json={"message": "Printer not found."},
                        request=request_obj,
                    )
                await self._fetch_subresources(request_client, base_url, handle, headers, printer)
                payload = self._entity_record(printer)
                return httpx.Response(200, json={"data": payload}, request=request_obj)

            response = await request_client.request(
                method, url, params=params, json=request_body, headers=headers
            )
            if response.status_code >= 400 or not response.content:
                return response
            try:
                data = response.json()
            except ValueError:
                return response
            if isinstance(data, dict) and "data" in data:
                data["data"] = (
                    self._entity_record(data["data"])
                    if isinstance(data["data"], dict)
                    else [
                        self._entity_record(item) if isinstance(item, dict) else item
                        for item in data["data"]
                    ]
                )
                return httpx.Response(response.status_code, json=data, request=response.request)
            return response

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400 or not response.content:
            return AdapterResponse(response.status_code, response.content, dict(response.headers))

        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(response.status_code, response.content, dict(response.headers))

        if isinstance(data, dict) and "data" in data:
            data["data"] = (
                self._entity_record(data["data"])
                if isinstance(data["data"], dict)
                else [
                    self._entity_record(item) if isinstance(item, dict) else item
                    for item in data["data"]
                ]
            )
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(response.status_code, content, {"content-type": "application/json"})

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
        if action_key != "printDocument":
            return _json_response(404, {"message": f"Unknown Printer action: {action_key}"})
        try:
            envelope = json.loads((body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _json_response(422, {"message": f"Invalid action envelope: {exc}"})
        if not isinstance(envelope, dict):
            return _json_response(422, {"message": "Action envelope must be a JSON object."})
        ids = envelope.get("ids")
        if not isinstance(ids, list) or not ids:
            return _json_response(422, {"message": "ids must be a non-empty array."})
        if len(ids) != 1:
            return _json_response(422, {"message": "printDocument only supports one Printer id."})
        printer_id = str(ids[0]).strip()
        command = envelope.get("command") or {}
        if not isinstance(command, dict):
            return _json_response(422, {"message": "command must be an object."})
        file_content = (
            command.get("fileContent") or command.get("file_content") or command.get("content")
        )
        if not isinstance(file_content, str) or not file_content.strip():
            return _json_response(422, {"message": "command.fileContent is required."})

        file_obj: dict[str, Any] = {"type": "pdf", "content": file_content}
        file_name = command.get("fileName") or command.get("file_name")
        if file_name:
            file_obj["name"] = str(file_name)
        payload: dict[str, Any] = {"printer": {"id": printer_id}, "file": file_obj}
        quantity = command.get("quantity")
        if quantity not in (None, ""):
            try:
                payload["quantity"] = int(quantity)
            except (TypeError, ValueError):
                return _json_response(422, {"message": "command.quantity must be an integer."})

        headers = _request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            return await request_client.post(
                f"{base_url.rstrip('/')}/api/v1/printJobs",
                json=payload,
                headers=headers,
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400:
            return AdapterResponse(response.status_code, response.content, dict(response.headers))

        job_id = self._response_id(response)
        return _json_response(
            200,
            {
                "message": f"Print job created on printer {printer_id}.",
                "data": {
                    "printerId": {"id": printer_id},
                    "printJobId": job_id,
                    "fileName": file_name,
                    "quantity": payload.get("quantity", 1),
                },
            },
        )
