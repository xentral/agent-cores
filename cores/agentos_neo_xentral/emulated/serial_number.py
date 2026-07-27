"""Xentral V3 facade · serialNumber — Seriennummer (docs/01-model.md §7.3).

Honest resource gap (docs/05 #4): batches/serial numbers/best-before exist only as
read-only v3 Product INCLUDES — there is no serialNumber resource, no list, no
trace, and maintenance happens through internal web routes only. This adapter
declares the target model (serial number with status + trace + the ``customer``
reference a unit carries after delivery, for warranty) so the entity and its shape
are visible; everything stays grey/blue until ``GET /v3/serialNumbers
(+/{id}/trace)`` ships upstream. Structurally identical to ``batch``.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop


class SerialNumberAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="SerialNumber",
        label_en="Serial number",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.serialNumber",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),  # nothing upstream yet (docs/05 #4)
    )
    v3_path = "/api/v3/serialNumbers"  # proposed endpoint — 404 until built
    include = ""
    preview_template = "{{number}}"
    sections = {"general": {"label": "General"}, "trace": {"label": "Trace"}}

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "block",
                        "Block",
                        wish="The serial-number API itself is missing upstream (the entity is blocked).",
                    ),
                    self.step_cmd(
                        "release",
                        "Release",
                        wish="The serial-number API itself is missing upstream (the entity is blocked).",
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "traceReport",
                "Trace report",
                wish="Batch/serial trace has no public API (05) — the report comes with it.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "number": prop(
                "string",
                "Number",
                **RO,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
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
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=[
                    {"value": "inStock", "label": "In stock"},
                    {"value": "delivered", "label": "Delivered"},
                    {"value": "returned", "label": "Returned"},
                ],
                filterable=True,
            ),
            "customer": prop(
                "reference",
                "Customer",
                reference="Customer",
                renderProperty="name",
                **RO,
                section="general",
                filterable=True,
            ),
            "trace": prop(
                "embedded",
                "Trace",
                **RO,
                section="trace",
                properties={
                    "goodsReceipts": prop(
                        "collection",
                        "Goods receipts",
                        **RO,
                        node={"properties": {"id": prop("string", "ID", **RO)}},
                    ),
                    "deliveryNotes": prop(
                        "collection",
                        "Delivery notes",
                        **RO,
                        node={"properties": {"id": prop("string", "ID", **RO)}},
                    ),
                },
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        # No upstream resource exists yet — passthrough placeholder.
        return {
            "object": "serialNumber",
            "id": (f"sn_{r.get('id')}" if r.get("id") is not None else None),
            **r,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        return {}, {k for k in model if k != "object"}
