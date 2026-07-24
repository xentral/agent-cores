"""Xentral V3 facade · stockMovement — Lagerbewegung (docs/01-model.md §7.4, ADR-010).

Honest resource gap — the BIGGEST one (docs/05 #1): the upstream has NOTHING to
read stock movements (no Lagerprotokoll API) and writing is only the absolute
``setTotalStock`` + implicit document bookings. This adapter declares the target
model (append-only movement with type/from/to/source/unitCost) so the entity and
its shape are visible; every operation stays grey/blue until
``GET+POST /v3/stockMovements`` ships upstream.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop

_TYPE_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("receipt", "issue", "transfer", "correction")
]


class StockMovementAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StockMovement",
        label_en="Stock movement",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.stockMovement",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),  # nothing upstream yet (docs/05 #1)
    )
    v3_path = "/api/v3/stockMovements"  # proposed endpoint — 404 until built
    include = ""
    preview_template = "{{product.name}}"
    sections = {"general": {"label": "General"}, "source": {"label": "Source"}}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "date": prop("datetime", "Date", **RO, section="general", sortable=True),
            "type": prop(
                "select",
                "Type",
                **RO,
                section="general",
                options=_TYPE_OPTIONS,
                filterable=True,
                previewable=True,
            ),
            "product": prop(
                "reference",
                "Product",
                reference="Product",
                renderProperty="name",
                section="general",
                filterable=True,
                previewable=True,
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
            "from": prop(
                "reference",
                "From location",
                reference="StorageLocation",
                renderProperty="name",
                **RO,
                section="general",
            ),
            "to": prop(
                "reference",
                "To location",
                reference="StorageLocation",
                renderProperty="name",
                **RO,
                section="general",
            ),
            "batch": prop(
                "reference",
                "Batch",
                reference="Batch",
                renderProperty="number",
                **RO,
                section="general",
            ),
            "unitCost": prop(
                "embedded",
                "Unit cost",
                **RO,
                section="general",
                properties={
                    "amount": prop("string", "Amount", **RO),
                    "currency": prop("string", "Currency", **RO),
                },
            ),
            "source": prop(
                "embedded",
                "Source",
                **RO,
                section="source",
                properties={
                    "document": prop("string", "Document", **RO),
                    "user": prop(
                        "reference", "User", reference="User", renderProperty="name", **RO
                    ),
                    "reason": prop("string", "Reason", **RO),
                },
            ),
            "bookedAt": prop("datetime", "Booked at", **RO, filterable=True, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        # No upstream payload exists yet — passthrough placeholder for the day it does.
        return {
            "object": "stockMovement",
            "date": r.get("bookedAt") or r.get("date"),
            "id": (f"stm_{r.get('id')}" if r.get("id") is not None else None),
            **r,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        return {}, {k for k in model if k != "object"}
