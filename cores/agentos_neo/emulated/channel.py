"""Xentral V3 facade · channel — Vertriebskanal (docs/01-model.md §6.4).

SECOND UPSTREAM (docs/02-ist-analyse §2b): reads the BF entity API
``GET /api/entity/salesChannel`` (verified live: id/uuid/name, full CRUD upstream)
— docs/05 #17 revised: the entity list exists; still missing upstream are the
channel TYPE, active flag, and the sync-run status (last run, success/failure),
which stay blue wishes.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop


class ChannelAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Channel",
        label_en="Channel",
        category="masterdata",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.channel",
        source_apis=("agentos_neo",),
        operations=("list", "read"),  # BF has CRUD; our write-mapping not verified yet
    )
    v3_path = "/api/entity/salesChannel"
    include = ""
    preview_template = "{{name}}"
    bf_sort = True
    sections = {"general": {"label": "General"}, "sync": {"label": "Sync"}}

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

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "pause", "Pause", wish="The channel API (05 #17) exposes no state writes."
                    ),
                    self.step_cmd(
                        "resume", "Resume", wish="The channel API (05 #17) exposes no state writes."
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "syncOrders",
                "Sync orders",
                wish="The channel API (05 #17) exposes no sync triggers.",
            ),
            self.action_def(
                "syncStock", "Sync stock", wish="The channel API (05 #17) exposes no sync triggers."
            ),
            self.action_def(
                "syncProducts",
                "Sync products",
                wish="The channel API (05 #17) exposes no sync triggers.",
            ),
            self.action_def(
                "testConnection",
                "Test connection",
                wish="The channel API (05 #17) exposes no connection test.",
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
            "defaults": prop(
                "embedded",
                "Defaults",
                **RO,
                section="general",
                properties={
                    "priceList": prop("reference", "Price list", **RO, reference="PriceList"),
                    "warehouse": prop("reference", "Warehouse", **RO, reference="Warehouse"),
                },
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
            "sync": prop(
                "embedded",
                "Sync",
                **RO,
                section="sync",
                properties={
                    "lastRunAt": prop("datetime", "Last run", **RO),
                    "status": prop("select", "Status", **RO),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "channel",
            "platform": None,
            "defaults": {"priceList": None, "warehouse": None},
            # NUMERIC self-id, matching the id v3 document relations carry, so a
            # document's channel reference and this record share one id. `get`
            # resolves the numeric id to the BF uuid via `_resolve_upstream_handle`
            # (X5), since the BF entity is fetched by uuid.
            "id": (f"ch_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "type": None,
            "active": None,
            "sync": {"lastRunAt": None, "status": None},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # BF salesChannel has CRUD (name) — write-mapping to be verified live next.
        return {}, {k for k in model if k not in {"object", "id", "sync", "createdAt", "updatedAt"}}
