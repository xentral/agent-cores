"""Xentral V3 facade · stockLevel — Bestand (docs/01-model.md §7.5).

The stock query of the model: a read-only projection of product × warehouse ×
storage location × batch with ``quantity``/``reserved``/``available``. Honest
resource gap (docs/05 #18): the data exists today but only via an inconvenient
v1/v2 fan-in (v1 CRUD + v2 items + v1 product-stocks); there is no single
``GET /v3/stockLevels?filter[product|warehouse|storageLocation|batch]`` yet. This
adapter declares the target projection so the entity and its filters are visible;
list stays grey/blue until the unified endpoint ships (or the fan-in is composed
here).
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop


class StockLevelAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StockLevel",
        label_en="Stock level",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.stockLevel",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),  # read-only projection; no unified endpoint yet (docs/05 #18)
    )
    v3_path = "/api/v3/stockLevels"  # proposed endpoint — 404 until built (fan-in not composed)
    include = ""
    preview_template = "{{product.name}}"
    sections = {"general": {"label": "General"}}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "product": prop(
                "reference",
                "Product",
                reference="Product",
                renderProperty="name",
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
                **RO,
                section="general",
                filterable=True,
            ),
            "storageLocation": prop(
                "reference",
                "Storage location",
                reference="StorageLocation",
                renderProperty="name",
                **RO,
                section="general",
                filterable=True,
            ),
            "batch": prop(
                "reference",
                "Batch",
                reference="Batch",
                renderProperty="number",
                **RO,
                section="general",
                filterable=True,
            ),
            "quantity": prop(
                "embedded",
                "Quantity",
                **RO,
                section="general",
                properties={
                    "value": prop("decimal", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
            "reserved": prop(
                "embedded",
                "Reserved",
                **RO,
                section="general",
                properties={
                    "value": prop("decimal", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
            "available": prop(
                "embedded",
                "Available",
                **RO,
                section="general",
                properties={
                    "value": prop("decimal", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        # No unified upstream projection exists yet — passthrough placeholder.
        return {
            "object": "stockLevel",
            "id": (f"slv_{r.get('id')}" if r.get("id") is not None else None),
            **r,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # A projection is not writable against the facade; corrections flow
        # through stockMovement / stockTake, not here.
        return {}, {k for k in model if k != "object"}
