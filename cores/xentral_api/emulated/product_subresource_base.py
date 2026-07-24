from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
import requests

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

_TIMEOUT_SECONDS = 60.0


def property_shape(type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "label": label, "language": "en", **extra}


def json_response(status_code: int, payload: dict[str, Any]) -> AdapterResponse:
    return AdapterResponse(
        status_code=status_code,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def request_headers(token: str, accept_language: str | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "xentral-ai-agent",
        "X-Pagination": "table",
    }
    if accept_language:
        headers["Accept-Language"] = accept_language
    return headers


def requests_fallback_response(
    *,
    method: str,
    url: str,
    params: list[tuple[str, str]],
    headers: dict[str, str],
    json_body: Any = None,
) -> httpx.Response:
    response = requests.request(
        method,
        url,
        params=params,
        headers=headers,
        json=json_body,
        timeout=_TIMEOUT_SECONDS,
    )
    return httpx.Response(
        response.status_code,
        content=response.content,
        headers=forward_headers(response.headers),
        request=httpx.Request(method, response.url),
    )


def forward_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in dict(headers).items()
        if key.lower() not in {"content-encoding", "content-length"}
    }


def ref(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        out = deepcopy(value)
        if out.get("id") is not None:
            out["id"] = str(out["id"])
        return out
    return {"id": str(value)}


def money(value: Any, currency: Any = None) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return {
            "amount": value.get("amount"),
            "currency": value.get("currency") or currency or "EUR",
        }
    return {"amount": value, "currency": currency or "EUR"}


def page_defaults(query: list[tuple[str, str]]) -> list[tuple[str, str]]:
    keys = {key for key, _ in query}
    out = list(query)
    if "page[number]" not in keys:
        out.append(("page[number]", "1"))
    if "page[size]" not in keys:
        out.append(("page[size]", "50"))
    return out


def filter_value(query: list[tuple[str, str]], filter_key: str) -> str | None:
    filter_index: str | None = None
    for key, value in query:
        if key.startswith("filter[") and key.endswith("][key]") and value == filter_key:
            filter_index = key[len("filter[") : key.index("]")]
            break
    if filter_index is None:
        return None
    needle = f"filter[{filter_index}][value]"
    for key, value in query:
        if key == needle:
            return value
    return None


def strip_filter(
    query: list[tuple[str, str]], filter_keys: tuple[str, ...]
) -> list[tuple[str, str]]:
    indexes: set[str] = set()
    for key, value in query:
        if key.startswith("filter[") and key.endswith("][key]") and value in filter_keys:
            indexes.add(key[len("filter[") : key.index("]")])
    if not indexes:
        return list(query)
    return [
        (key, value)
        for key, value in query
        if not any(key.startswith(f"filter[{index}]") for index in indexes)
    ]


class ProductSubresourceAdapterBase:
    manifest: EmulationManifest
    base_path: str
    read_path_template: str | None = None
    product_child_path: str | None = None
    create_path_template: str | None = None
    update_path_template: str | None = None
    delete_path_template: str | None = None
    product_filter_keys: tuple[str, ...] = ("product", "productId", "parentProduct")
    payload_read_only_fields: set[str] = {"id", "uuid", "createdAt", "updatedAt"}
    query_aliases: dict[str, str] = {}

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "previewTemplateString": self.preview_template(),
            "sections": self.sections(),
            "rootNode": {"properties": self.properties()},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    def preview_template(self) -> str:
        return "{{id}}"

    def sections(self) -> dict[str, dict[str, str]]:
        return {
            "general": {"label": "General"},
            "pricing": {"label": "Pricing"},
            "validity": {"label": "Validity"},
            "references": {"label": "References"},
        }

    def properties(self) -> dict[str, Any]:
        raise NotImplementedError

    def normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def payload(self, body: bytes | None) -> Any:
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{self.manifest.key} payload must be a JSON object")
        return {
            key: deepcopy(value)
            for key, value in data.items()
            if key not in self.payload_read_only_fields
        }

    def _query(self, query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        translated: list[tuple[str, str]] = []
        for key, value in query:
            lookup = key[:-6] if key.endswith("[key]") else key
            if key.endswith("[key]"):
                value = self.query_aliases.get(value, value)
            elif lookup in self.query_aliases and value == "":
                value = self.query_aliases[lookup]
            translated.append((key, value))
        return page_defaults(translated)

    def _product_id_from_body(self, body: Any) -> str | None:
        if not isinstance(body, dict):
            return None
        for key in self.product_filter_keys:
            value = body.get(key)
            if isinstance(value, dict) and value.get("id") is not None:
                return str(value["id"])
            if value not in (None, ""):
                return str(value)
        return None

    def _product_id_from_query(self, query: list[tuple[str, str]]) -> str | None:
        for key in self.product_filter_keys:
            value = filter_value(query, key)
            if value:
                return value
        return None

    def _list_path_and_params(
        self, query: list[tuple[str, str]]
    ) -> tuple[str, list[tuple[str, str]]]:
        params = self._query(query)
        product_id = self._product_id_from_query(params)
        if product_id and self.product_child_path:
            filtered = strip_filter(params, self.product_filter_keys)
            return self.product_child_path.format(product_id=product_id), page_defaults(filtered)
        return self.base_path, params

    def _write_path(self, method: str, handle: str | None, payload: Any) -> str:
        if method == "POST":
            product_id = self._product_id_from_body(payload)
            if product_id and self.create_path_template:
                return self.create_path_template.format(product_id=product_id)
            return self.base_path
        if method in {"PATCH", "PUT"}:
            product_id = self._product_id_from_body(payload)
            if product_id and self.update_path_template:
                return self.update_path_template.format(product_id=product_id)
            return f"{self.base_path}/{handle}" if handle else self.base_path
        if method == "DELETE" and handle:
            if self.delete_path_template:
                product_id = self._product_id_from_body(payload)
                return self.delete_path_template.format(product_id=product_id, handle=handle)
            return f"{self.base_path}/{handle}"
        return self.base_path

    def wire_payload(self, payload: Any) -> Any:
        return payload

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
        headers = request_headers(token, accept_language)
        request_body: Any = None
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            try:
                request_body = self.payload(body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return json_response(
                    400, {"title": f"Invalid {self.manifest.key} payload", "detail": str(exc)}
                )

        if method == "GET" and handle:
            path = (
                self.read_path_template.format(handle=handle)
                if self.read_path_template
                else f"{self.base_path}/{handle}"
            )
            params = self._query(query)
        elif method == "GET":
            path, params = self._list_path_and_params(query)
        else:
            path = self._write_path(method, handle, request_body)
            request_body = self.wire_payload(request_body)
            params = self._query(query)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            url = f"{base_url.rstrip('/')}{path}"
            try:
                if method == "GET":
                    return await request_client.get(url, params=params, headers=headers)
                return await request_client.request(
                    method, url, params=params, json=request_body, headers=headers
                )
            except httpx.DecodingError:
                if method != "GET":
                    raise
                return requests_fallback_response(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400 or not response.content:
            return AdapterResponse(
                response.status_code, response.content, forward_headers(response.headers)
            )

        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code, response.content, forward_headers(response.headers)
            )

        if isinstance(data, dict) and "data" in data:
            if isinstance(data["data"], dict):
                data["data"] = self.normalize_record(data["data"])
            elif isinstance(data["data"], list):
                data["data"] = [
                    self.normalize_record(item) if isinstance(item, dict) else item
                    for item in data["data"]
                ]
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(response.status_code, content, {"content-type": "application/json"})
