"""Xentral V3 facade · printer — the instance's printers, with real printing.

Reads ``GET /api/v1/printers`` (undocumented in the public OpenAPI but live —
verified on mvp: id, name, designation, active, connection, useCase,
printMethod, spooler, deviceId; standard v1 page[…] dialect; NO ``/{id}`` GET,
so the entity is list-only). Ported from the xentral_api core's Printer entity
so printing works through ``xentral_erp_core`` on this core too.

The ``printDocument`` action creates a REAL print job
(``POST /api/v1/printJobs`` — public OpenAPI): base64 PDF + optional file name
and quantity on the target printer. Printer CRUD and the print-job/setup
fan-in the classic core carries are follow-ups, not ported yet.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, prop

_CONNECTIONS = ("cups", "pdf", "download", "nextgen_spooler", "spooler", "adapterbox")
_USE_CASES = ("shipping-labels", "shipping-documents", "other")


class PrinterAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Printer",
        label_en="Printer",
        category="settings",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.printer",
        source_apis=("agentos_neo_xentral",),
        operations=("list",),  # upstream has no GET /{id}
    )
    v3_path = "/api/v1/printers"
    include = ""
    preview_template = "{{name}}"
    v1_paging = True
    sort_tiebreak = None
    sections = {"general": {"label": "General"}}

    def actions(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "printDocument",
                "label": "Print document",
                "bulk": False,
                "method": "PATCH",
                "path": "/api/entity/Printer/actions/printDocument",
                "destructive": True,
                "description": (
                    "Create a REAL print job for a base64-encoded PDF on this "
                    "printer (paper comes out — confirm intent first)."
                ),
                "command": {
                    "type": "object",
                    "required": ["fileContent"],
                    "properties": {
                        "fileContent": {
                            "type": "string",
                            "label": "PDF (base64)",
                            "description": "Base64-encoded PDF file data.",
                        },
                        "fileName": {"type": "string", "label": "File name"},
                        "quantity": {
                            "type": "integer",
                            "label": "Quantity",
                            "default": 1,
                        },
                    },
                },
            }
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "designation": prop("string", "Designation", **RO, section="general"),
            "active": prop("boolean", "Active", **RO, section="general", previewable=True),
            "connection": prop(
                "select",
                "Connection type",
                **RO,
                section="general",
                options=[{"value": v, "label": v.replace("_", " ")} for v in _CONNECTIONS],
            ),
            "useCase": prop(
                "select",
                "Use case",
                **RO,
                section="general",
                options=[{"value": v, "label": v.replace("-", " ")} for v in _USE_CASES],
            ),
            "printMethod": prop("string", "Print method", **RO, section="general"),
            "spooler": prop("string", "Spooler", **RO, section="general"),
            "deviceId": prop("string", "Device ID", **RO, section="general"),
            "createdAt": prop("datetime", "Created at", **RO, section="general"),
            "updatedAt": prop("datetime", "Updated at", **RO, section="general"),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "printer",
            "id": (f"prn_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "designation": r.get("designation"),
            "active": r.get("active"),
            "connection": r.get("connection"),
            "useCase": r.get("useCase"),
            "printMethod": r.get("printMethod"),
            "spooler": r.get("spooler"),
            "deviceId": r.get("deviceId"),
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

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
            return await super().action(
                action_key=action_key,
                handle=handle,
                body=body,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if not ids:
            return self._refuse(422, "printDocument needs a target printer id (ids[])")
        printer_id = str(ids[0])
        if "_" in printer_id:
            printer_id = printer_id.split("_", 1)[1]
        command = envelope.get("command") or {}
        file_content = command.get("fileContent") or command.get("file_content")
        if not isinstance(file_content, str) or not file_content.strip():
            return self._json(
                422, {"title": "printDocument requires command.fileContent (base64 PDF)."}
            )
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
                return self._refuse(422, "command.quantity must be an integer.")

        headers = self._headers(token, accept_language)
        url = f"{base_url.rstrip('/')}/api/v1/printJobs"

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.post(url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        if resp.status_code >= 400:
            return AdapterResponse(
                resp.status_code, resp.content, {"content-type": "application/json"}
            )
        try:
            job = (resp.json() or {}).get("data") or {}
        except ValueError:
            job = {}
        return self._json(
            200,
            {
                "data": {
                    "printer": f"prn_{printer_id}",
                    "printJobId": job.get("id"),
                    "fileName": file_name,
                    "quantity": payload.get("quantity", 1),
                    "message": f"Print job created on printer prn_{printer_id}.",
                }
            },
        )
