"""Xentral V3 facade · tag — Tag-Katalog (Stammdaten).

SECOND UPSTREAM (docs/02-ist-analyse §2b): reads the BF entity API
``GET /api/entity/tag`` (verified live: slug, label, color, tagGroup). This is the
catalogue of available tags an agent needs before tagging documents — documents
themselves carry ``tags`` as a plain title array (docs/01-model.md §6.1). BF has
CRUD upstream; our write-mapping is pending (wish).
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop


class TagAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Tag",
        label_en="Tag",
        category="masterdata",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.tag",
        source_apis=("agentos_neo",),
        operations=("list", "read"),  # BF has CRUD; our write-mapping not verified yet
    )
    v3_path = "/api/entity/tag"
    include = ""
    preview_template = "{{label}}"
    bf_sort = True
    sections = {"general": {"label": "General"}}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "label": prop(
                "string",
                "Label",
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "slug": prop("string", "Slug", **RO, section="general", filterable=True),
            "color": prop("string", "Color", section="general", previewable=True),
            "group": prop("string", "Group", **RO, section="general", filterable=True),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        tg = r.get("tagGroup")
        return {
            "object": "tag",
            # BF entities are fetched by uuid (GET /{id} 404s, GET /{uuid} 200);
            # encode the uuid so the speaking id round-trips through `get` (F3).
            "id": (
                f"tag_{r['uuid']}"
                if r.get("uuid")
                else (f"tag_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "label": r.get("label"),
            "slug": r.get("slug"),
            "color": r.get("color"),
            "group": (tg.get("label") or tg.get("group")) if isinstance(tg, dict) else tg,
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
