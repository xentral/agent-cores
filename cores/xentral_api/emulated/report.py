from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx

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


class ReportAdapter:
    manifest = EmulationManifest(
        key="Report",
        label_en="Report",
        category="Analytics",
        rollout_batch="analytics-report-v1",
        adapter="v1-analytics-report",
        source_apis=(
            "/api/v1/analytics/report",
            "/api/v1/analytics/report/{id}/query",
            "/api/v1/analytics/report/{id}/export",
            "/api/v1/analytics/collection",
        ),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v1/analytics/report"
    payload_read_only_fields = {
        "id",
        "uuid",
        "createdAt",
        "updatedAt",
        "lastEditedAt",
        "lastRunAt",
        "result",
        "exports",
        "schedules",
        "permalinks",
    }

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        p = _property
        properties: dict[str, Any] = {
            "id": p(
                "string", "ID", access="readOnly", filterable=True, sortable=True, previewable=True
            ),
            "uuid": p("string", "UUID", access="readOnly"),
            "title": p(
                "string",
                "Title",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
                rules=["required"],
            ),
            "description": p(
                "string", "Description", searchable=True, previewable=True, section="general"
            ),
            "collection": p(
                "reference",
                "Collection",
                reference="ReportCollection",
                renderProperty="name",
                filterable=True,
                section="general",
            ),
            "collectionId": p(
                "reference",
                "Collection",
                reference="ReportCollection",
                renderProperty="name",
                filterable=True,
                section="general",
                rules=["required"],
            ),
            "sqlString": p("string", "SQL string", section="query", rules=["required"]),
            "parameters": p(
                "collection",
                "Parameters",
                section="query",
                node={"properties": self._parameter_properties()},
            ),
            "isFavorite": p("boolean", "Favorite", filterable=True, section="general"),
            "isReadOnly": p(
                "boolean", "Read only", access="readOnly", filterable=True, section="system"
            ),
            "userId": p("integer", "User ID", access="readOnly", filterable=True, section="system"),
            "lastEditedBy": p("string", "Last edited by", access="readOnly", section="system"),
            "createdAt": p(
                "datetime", "Created at", access="readOnly", sortable=True, section="system"
            ),
            "updatedAt": p(
                "datetime", "Updated at", access="readOnly", sortable=True, section="system"
            ),
            "lastRunAt": p(
                "datetime", "Last run at", access="readOnly", sortable=True, section="system"
            ),
            "result": p(
                "embedded",
                "Last result",
                access="readOnly",
                section="result",
                properties={
                    "columns": p(
                        "collection", "Columns", node={"properties": {"name": p("string", "Name")}}
                    ),
                    "rows": p("collection", "Rows", node={"properties": {}}),
                    "rowCount": p("integer", "Row count"),
                },
            ),
            "exports": p(
                "collection",
                "Exports",
                access="readOnly",
                section="exports",
                node={"properties": self._export_properties()},
            ),
            "schedules": p(
                "collection",
                "Schedules",
                access="readOnly",
                section="schedules",
                node={"properties": self._schedule_properties()},
            ),
            "permalinks": p(
                "collection",
                "Permalinks",
                access="readOnly",
                section="sharing",
                node={"properties": self._permalink_properties()},
            ),
        }
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            # Running/exporting a report executes it and returns output — it does
            # not change the Report entity's own state, so these are actions, not
            # process-step commands.
            "actions": self._actions(),
            "previewTemplateString": "{{title}}",
            "sections": {
                "general": {"label": "General"},
                "query": {"label": "Query"},
                "result": {"label": "Result"},
                "exports": {"label": "Exports"},
                "schedules": {"label": "Schedules"},
                "sharing": {"label": "Sharing"},
                "system": {"label": "System"},
            },
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    @staticmethod
    def _parameter_properties() -> dict[str, Any]:
        return {
            "name": _property("string", "Name"),
            "type": _property("string", "Type"),
            "defaultValue": _property("string", "Default value"),
            "value": _property("string", "Value"),
        }

    @staticmethod
    def _export_properties() -> dict[str, Any]:
        return {
            "uuid": _property("string", "UUID", access="readOnly"),
            "status": _property("string", "Status", access="readOnly"),
            "downloadUrl": _property("string", "Download URL", access="readOnly"),
            "format": _property("string", "Format", access="readOnly"),
            "createdAt": _property("datetime", "Created at", access="readOnly"),
        }

    @staticmethod
    def _schedule_properties() -> dict[str, Any]:
        return {
            "uuid": _property("string", "UUID", access="readOnly"),
            "frequency": _property("string", "Frequency"),
            "recipients": _property("collection", "Recipients", node={"properties": {}}),
            "format": _property("string", "Format"),
        }

    @staticmethod
    def _permalink_properties() -> dict[str, Any]:
        return {
            "token": _property("string", "Token", access="readOnly"),
            "url": _property("string", "URL", access="readOnly"),
            "expiresAt": _property("datetime", "Expires at"),
        }

    @staticmethod
    def _run_command_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "parameters": {
                    "type": "array",
                    "label": "Parameters",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "label": "Name"},
                            "type": {"type": "string", "label": "Type"},
                            "defaultValue": {"type": "string", "label": "Default value"},
                            "value": {"type": "string", "label": "Value"},
                        },
                    },
                },
                "settings": {"type": "object", "label": "Settings"},
            },
        }

    @classmethod
    def _export_command_schema(cls) -> dict[str, Any]:
        schema = cls._run_command_schema()
        schema["properties"]["settings"] = {
            "type": "object",
            "label": "Settings",
            "properties": {
                "exportFormat": {
                    "type": "string",
                    "label": "Export format",
                    "enum": ["csv", "xlsx", "json"],
                    "default": "csv",
                },
                "delimiter": {"type": "string", "label": "Delimiter", "default": ","},
            },
        }
        return schema

    @classmethod
    def _actions(cls) -> list[dict[str, Any]]:
        return [
            {
                "key": "runReport",
                "label": "Run report",
                "bulk": False,
                "method": "PATCH",
                "path": "/api/entity/Report/actions/runReport",
                "destructive": False,
                "description": "Run the selected Analytics report and return the result table.",
                "command": cls._run_command_schema(),
            },
            {
                "key": "exportReport",
                "label": "Export report",
                "bulk": False,
                "method": "PATCH",
                "path": "/api/entity/Report/actions/exportReport",
                "destructive": False,
                "description": "Start an asynchronous export for the selected Analytics report.",
                "command": cls._export_command_schema(),
            },
        ]

    @classmethod
    def _query(cls, query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        aliases = {
            "collection": "collectionId",
            "collectionId": "collectionId",
            "title": "title",
            "description": "description",
            "isFavorite": "isFavorite",
            "isReadOnly": "isReadOnly",
            "createdAt": "createdAt",
            "updatedAt": "updatedAt",
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
    def _collection_ref(record: dict[str, Any]) -> dict[str, str] | None:
        collection = record.get("collection")
        if isinstance(collection, dict) and collection.get("id") not in (None, ""):
            return {"id": str(collection["id"])}
        collection_id = record.get("collectionId") or record.get("collection_id")
        if collection_id not in (None, ""):
            return {"id": str(collection_id)}
        return None

    @classmethod
    def _entity_record(cls, raw: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(raw)
        if record.get("id") is not None:
            record["id"] = str(record["id"])
            record["uuid"] = record.get("uuid") or record["id"]
        collection_ref = cls._collection_ref(record)
        if collection_ref is not None:
            record["collection"] = collection_ref
            record["collectionId"] = collection_ref
        if "sql_string" in record and "sqlString" not in record:
            record["sqlString"] = record.pop("sql_string")
        if "last_edited_by" in record and "lastEditedBy" not in record:
            record["lastEditedBy"] = record.pop("last_edited_by")
        if "created_at" in record and "createdAt" not in record:
            record["createdAt"] = record.pop("created_at")
        if "updated_at" in record and "updatedAt" not in record:
            record["updatedAt"] = record.pop("updated_at")
        allowed = set(cls().metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in record.items() if key in allowed and value is not None}

    @classmethod
    def _report_payload(cls, body: bytes | None) -> dict[str, Any]:
        if not body:
            return {"report": {}}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Report payload must be a JSON object")
        report = payload.get("report") if isinstance(payload.get("report"), dict) else payload
        out = {
            key: deepcopy(value)
            for key, value in report.items()
            if key not in cls.payload_read_only_fields
        }
        collection = out.pop("collection", None)
        if collection is not None and "collectionId" not in out:
            if isinstance(collection, dict):
                out["collectionId"] = collection.get("id")
            else:
                out["collectionId"] = collection
        if isinstance(out.get("collectionId"), dict):
            out["collectionId"] = out["collectionId"].get("id")
        if out.get("collectionId") not in (None, ""):
            out["collectionId"] = int(out["collectionId"])
        return {"report": out}

    @staticmethod
    def _forward_headers(response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key in ("content-type", "etag", "cache-control", "x-pagination")
            if (value := response.headers.get(key))
        }

    @classmethod
    def _wrap_response(cls, response: httpx.Response) -> AdapterResponse:
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
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            return _json_response(405, {"message": f"Unsupported Report method: {method}"})
        if handle and not str(handle).isdigit():
            return _json_response(
                400, {"title": "Invalid Report handle", "detail": "Expected the numeric report id."}
            )
        path = self.base_path if not handle else f"{self.base_path}/{handle}"
        params = self._query(query)

        request_body: dict[str, Any] | None = None
        if method in {"POST", "PATCH", "PUT"}:
            try:
                request_body = self._report_payload(body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return _json_response(400, {"title": "Invalid Report payload", "detail": str(exc)})

        headers = _request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            return await request_client.request(
                method,
                f"{base_url.rstrip('/')}{path}",
                params=params,
                json=request_body,
                headers=headers,
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if method == "DELETE" and (response.status_code == 204 or not response.content):
            return _json_response(200, {"deleted": True, "reportId": handle})
        return self._wrap_response(response)

    @staticmethod
    def _action_envelope(
        body: bytes | None, handle: str | None
    ) -> tuple[str, dict[str, Any]] | AdapterResponse:
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
            return _json_response(422, {"message": "Report actions only support one report id."})
        report_id = str(ids[0]).strip()
        if not report_id.isdigit():
            return _json_response(422, {"message": "Report id must be numeric."})
        command = envelope.get("command") or {}
        if not isinstance(command, dict):
            return _json_response(422, {"message": "command must be an object."})
        return report_id, command

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
        if action_key not in {"runReport", "exportReport"}:
            return _json_response(404, {"message": f"Unknown Report action: {action_key}"})
        parsed = self._action_envelope(body, handle)
        if isinstance(parsed, AdapterResponse):
            return parsed
        report_id, command = parsed
        payload = {
            key: value
            for key, value in {
                "parameters": command.get("parameters"),
                "settings": command.get("settings"),
            }.items()
            if value is not None
        }
        suffix = "query" if action_key == "runReport" else "export"
        headers = _request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            return await request_client.post(
                f"{base_url.rstrip('/')}{self.base_path}/{report_id}/{suffix}",
                json=payload,
                headers=headers,
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400:
            return AdapterResponse(
                response.status_code, response.content, self._forward_headers(response)
            )
        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code, response.content, self._forward_headers(response)
            )
        return _json_response(
            200,
            {
                "message": "Report executed."
                if action_key == "runReport"
                else "Report export started.",
                "data": {
                    "report": {"id": report_id},
                    "result": data,
                },
            },
        )
