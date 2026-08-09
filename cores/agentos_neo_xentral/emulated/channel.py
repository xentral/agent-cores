"""Xentral V3 facade · channel — Vertriebskanal (docs/01-model.md §6.4).

SECOND UPSTREAM (docs/02-ist-analyse §2b): reads the BF entity API
``GET /api/entity/salesChannel`` (verified live: id/uuid/name, full CRUD
upstream), ENRICHED per request from ``GET /api/v2/salesChannels`` (public
OpenAPI: active, moduleName, …) — that fills ``active`` directly and derives
``platform`` from the module name. Still missing upstream are the channel TYPE
and the sync-run status (last run, success/failure), which stay blue wishes
(docs/05 #17 revised).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, prop


class ChannelAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Channel",
        label_en="Channel",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.channel",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),  # BF has CRUD; our write-mapping not verified yet
    )
    v3_path = "/api/entity/salesChannel"
    include = ""
    preview_template = "{{name}}"
    bf_sort = True
    sections = {"general": {"label": "General"}}

    # Per-tenant {numeric channel id -> BF uuid} index. The channel speaking id
    # is the NUMERIC id (the form v3 document relations carry, so a document's
    # channel reference and the Channel record share one id), but the BF
    # salesChannel entity is fetched by uuid and rejects id-filtering. This index
    # bridges the two so `get` (self and cross-reference) resolves (X5). The set
    # is small and rarely changes; rebuilt on a cache miss.
    _uuid_index: dict[str, dict[str, str]] = {}

    async def _build_uuid_index(
        self, base_url: str, token: str, accept_language: str | None, client
    ) -> dict[str, str]:
        _, payload = await self._get(
            base_url,
            token,
            handle=None,
            query=[("page[number]", "1"), ("page[size]", "250")],
            accept_language=accept_language,
            client=client,
        )
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        idx = {
            str(r["id"]): str(r["uuid"])
            for r in rows
            if isinstance(r, dict) and r.get("id") is not None and r.get("uuid")
        }
        type(self)._uuid_index[base_url] = idx
        return idx

    async def _resolve_upstream_handle(
        self, handle: str, *, base_url: str, token: str, accept_language: str | None, client
    ) -> str:
        h = str(handle)
        if "-" in h:  # already a uuid (e.g. a direct ch_<uuid> — pass through)
            return handle
        idx = type(self)._uuid_index.get(base_url)
        if idx is None or h not in idx:  # cold cache or an unseen (new) channel
            idx = await self._build_uuid_index(base_url, token, accept_language, client)
        return idx.get(h, handle)

    # moduleName → the model's platform enum; best-effort normalization
    # ("shopimporter_shopify" / "shopify" → "shopify"), unknown modules stay null.
    _PLATFORMS = ("shopify", "shopware", "amazon", "ebay", "otto", "pos", "api")

    @classmethod
    def _platform_from_module(cls, module_name: Any) -> str | None:
        if not module_name:
            return None
        m = str(module_name).lower()
        for p in cls._PLATFORMS:
            if p in m:
                return p
        return None

    async def _v2_rows(
        self,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> dict[str, dict[str, Any]]:
        """Fresh ``/api/v2/salesChannels`` rows keyed by numeric id. Fetched per
        request (no cache — the set is small and ``active`` toggles must show
        immediately); an upstream failure degrades to no enrichment, not an error."""
        url = f"{base_url.rstrip('/')}/api/v2/salesChannels"
        params = [("page[number]", "1"), ("page[size]", "50")]
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.get(url, params=params, headers=headers)

        try:
            if client is None:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                    resp = await _do(c)
            else:
                resp = await _do(client)
            rows = (resp.json().get("data") or []) if resp.status_code < 400 else []
        except (httpx.HTTPError, ValueError):
            return {}
        return {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id") is not None}

    def _enrich(self, record: dict[str, Any], v2: dict[str, dict[str, Any]]) -> None:
        rid = str(record.get("id") or "")
        row = v2.get(rid.split("_", 1)[1]) if rid.startswith("ch_") else None
        if not row:
            return
        record["active"] = row.get("active")
        record["platform"] = self._platform_from_module(row.get("moduleName"))

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
        resp = await super().request(
            method=method,
            handle=handle,
            query=query,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if method.upper() != "GET" or resp.status_code >= 400:
            return resp
        try:
            payload = json.loads(resp.content or b"{}")
        except ValueError:
            return resp
        if not isinstance(payload, dict):
            return resp
        data = payload.get("data")
        v2 = await self._v2_rows(base_url, token, accept_language, client)
        if not v2:
            return resp
        if isinstance(data, dict):
            self._enrich(data, v2)
        elif isinstance(data, list):
            for rec in data:
                if isinstance(rec, dict):
                    self._enrich(rec, v2)
        return self._json(resp.status_code, payload)

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "pause", "Pause", wish=True
                    ),
                    self.step_cmd(
                        "resume", "Resume", wish=True
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "syncOrders",
                "Sync orders",
                wish=True,
            ),
            self.action_def(
                "syncStock", "Sync stock", wish=True
            ),
            self.action_def(
                "syncProducts",
                "Sync products",
                wish=True,
            ),
            self.action_def(
                "testConnection",
                "Test connection",
                wish=True,
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "platform": prop(
                "select",
                "Platform",
                **RO,
                section="general",
                options=[
                    {"value": v, "label": v.capitalize()}
                    for v in (
                        "shopify",
                        "shopware",
                        "amazon",
                        "ebay",
                        "otto",
                        "pos",
                        "direct",
                        "api",
                    )
                ],
            ),
            "name": prop(
                "string",
                "Name",
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "type": prop(
                "select",
                "Type",
                **RO,
                section="general",
                previewable=True,
                options=[
                    {"value": v, "label": v.capitalize()}
                    for v in ("shop", "marketplace", "direct", "pos")
                ],
            ),
            "active": prop("boolean", "Active", **RO, section="general"),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "channel",
            "platform": None,
            # NUMERIC self-id, matching the id v3 document relations carry, so a
            # document's channel reference and this record share one id. `get`
            # resolves the numeric id to the BF uuid via `_resolve_upstream_handle`
            # (X5), since the BF entity is fetched by uuid.
            "id": (f"ch_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "type": None,
            "active": None,
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # BF salesChannel has CRUD (name) — write-mapping to be verified live next.
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
