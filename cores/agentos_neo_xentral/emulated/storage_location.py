"""Xentral V3 facade · storageLocation — Lagerplatz (docs/01-model.md §7.5).

Reads ``GET /v1/storageLocations`` (verified live — thin: id, designation,
warehouse). The target model's kind/pickingOrder/capacity/contents need the
scattered v1/v2 fan-in (docs/03: v1 CRUD + v2 items + v1 product-stocks) — not
yet composed, so those blocks are blue wishes. ``stockLevel`` as THE stock query
is a separate upstream ask (docs/05 #18).
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref


class StorageLocationAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StorageLocation",
        label_en="Storage location",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.storageLocation",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),
    )
    v3_path = "/api/v1/storageLocations"
    include = ""
    preview_template = "{{name}}"
    query_aliases = {"name": "designation"}
    v1_paging = True
    sections = {"general": {"label": "General"}, "contents": {"label": "Contents"}}

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "block",
                        "Block",
                        wish="v1 storageLocations exposes no block/release state write.",
                    ),
                    self.step_cmd(
                        "release",
                        "Release",
                        wish="v1 storageLocations exposes no block/release state write.",
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "printLabel", "Print label", wish="Storage-location labels have no public endpoint."
            ),
            self.action_def(
                "requestCount",
                "Request count",
                wish="Spot counts have no public trigger; v1 storageLocations/setTotalStock only sets an absolute quantity.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=[
                    {"value": "active", "label": "Active"},
                    {"value": "blocked", "label": "Blocked"},
                ],
            ),
            "name": prop(
                "string",
                "Name",
                **RO,
                section="general",
                filterable=True,
                previewable=True,
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="general",
                previewable=True,
            ),
            "kind": prop(
                "select",
                "Kind",
                **RO,
                section="general",
                options=[
                    {"value": v, "label": v.capitalize()}
                    for v in ("picking", "bulk", "inbound", "returns", "quarantine")
                ],
            ),
            "pickingOrder": prop("integer", "Picking order", **RO, section="general"),
            "capacity": prop(
                "embedded",
                "Capacity",
                **RO,
                section="general",
                properties={
                    "maxWeight": prop(
                        "embedded",
                        "Max weight",
                        **RO,
                        properties={
                            "value": prop("decimal", "Value", **RO),
                            "unit": prop("string", "Unit", **RO),
                        },
                    ),
                    "note": prop("string", "Note", **RO),
                },
            ),
            "contents": prop(
                "collection",
                "Contents",
                **RO,
                section="contents",
                node={
                    "properties": {
                        "product": prop(
                            "reference", "Product", reference="Product", renderProperty="name", **RO
                        ),
                        "batch": prop(
                            "reference", "Batch", reference="Batch", renderProperty="number", **RO
                        ),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                        "reserved": prop(
                            "embedded",
                            "Reserved",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                    }
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        wh = r.get("warehouse")
        return {
            "object": "storageLocation",
            "status": ("blocked" if r.get("isRestrictedLocation") else "active"),
            "id": (f"loc_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
            "warehouse": ref(
                "wh_",
                wh.get("id") if isinstance(wh, dict) else wh,
                None,
                wh.get("name") if isinstance(wh, dict) else None,
                "warehouses",
            ),
            "kind": None,
            "pickingOrder": None,
            "capacity": {"maxWeight": None, "note": None},
            "contents": [],
            "createdAt": None,
            "updatedAt": None,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # v1 CRUD exists upstream but is not orchestrated here yet.
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
