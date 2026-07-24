from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest
from .tag_action import execute_tag_action, tag_action_metadata

_TIMEOUT_SECONDS = 60.0


def _property(type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "label": label, **extra}


class SupplierInvoiceAdapter:
    """Xentral Basic facade for native supplier invoices.

    CRUD stays on Xentral's native Business Entity endpoint. Basic owns the
    metadata/action surface so tags are exposed consistently with the other
    document-like entities in this core.
    """

    manifest = EmulationManifest(
        key="supplierInvoice",
        label_en="Supplier Invoice",
        category="Accounting",
        rollout_batch="basic-supplier-invoice-tags-v1",
        adapter="basic-native-supplier-invoice",
        source_apis=("/api/entity/supplierInvoice",),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/entity/supplierInvoice"
    detail_include = "tags"

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        properties = {
            "uuid": _property("string", "ID", access="readOnly"),
            "id": _property("string", "ID", access="readOnly"),
            "documentNumber": _property(
                "string",
                "Document number",
                access="readOnly",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "documentStatus": _property(
                "select",
                "Status",
                access="readOnly",
                filterable=True,
                previewable=True,
            ),
            "tags": _property(
                "collection",
                "Tags",
                section="segmentation",
                node={
                    "properties": {
                        "id": _property("string", "ID", access="readOnly"),
                        "name": _property("string", "Name"),
                        "color": _property("string", "Color"),
                    }
                },
            ),
        }
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{documentNumber}}",
            "sections": {
                "general": {"label": "General"},
                "segmentation": {"label": "Segmentation"},
            },
            "rootNode": {"properties": properties},
            "actions": [
                tag_action_metadata(self.manifest.key, "addTag"),
                tag_action_metadata(self.manifest.key, "removeTag"),
            ],
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

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

    @staticmethod
    def _json_response(status_code: int, payload: dict[str, Any]) -> AdapterResponse:
        return AdapterResponse(
            status_code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            {"content-type": "application/json"},
        )

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
        path = self.base_path if not handle else f"{self.base_path}/{handle}"
        headers = self._request_headers(token, accept_language)

        async def _run(request_client: httpx.AsyncClient) -> httpx.Response:
            return await request_client.request(
                method.upper(),
                f"{base_url.rstrip('/')}{path}",
                params=query,
                content=body,
                headers=headers,
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _run(request_client)
        else:
            response = await _run(client)
        return AdapterResponse(response.status_code, response.content, dict(response.headers))

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
        return await execute_tag_action(
            action_key=action_key,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
            entity_key=self.manifest.key,
            entity_label=self.manifest.label("en"),
            base_path=self.base_path,
            detail_include=self.detail_include,
            request_headers=self._request_headers,
            json_response=self._json_response,
        )
