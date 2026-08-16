"""Xentral V3 facade · batch — Charge (docs/01-model.md §7.3).

Honest resource gap (docs/05 #4): batches/serial numbers/best-before exist only as
read-only v3 Product INCLUDES — there is no batch resource, no list, no trace, and
maintenance happens through internal web routes only. This adapter declares the
target model (batch with stock + trace; serialNumber is structurally identical) so
the entity and its shape are visible; everything stays grey/blue until
``GET /v3/batches|serialNumbers (+/{id}/trace)`` ships upstream.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop


class BatchAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Batch",
        label_en="Batch",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.batch",
        source_apis=("agentos_neo_xentral",),
        # NOTHING is executable: /v3/batches does not exist (docs/05 #4, verified
        # 404 on mvp). Declaring list/read anyway made every caller discover the
        # gap by hitting a route that is not there — an unreadable 404 from the
        # upstream instead of an answer from the contract. The operations list is
        # the contract, so it now says so up front; the model below stays declared
        # as the target shape (ADR-014), and the gate answers 405 with the reason.
        operations=(),
        description=(
            "DECLARED BUT NOT READABLE: Xentral exposes no batch resource — batches "
            "exist only as read-only includes on a product. Batch-level stock is not "
            "queryable; use Product/StockLevel for quantities."
        ),
    )
    v3_path = "/api/v3/batches"  # proposed endpoint — 404 until built
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
                        wish=True,
                    ),
                    self.step_cmd(
                        "release",
                        "Release",
                        wish=True,
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def("traceReport", "Trace report", wish=True),
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
            "bestBefore": prop(
                "date", "Best before", **RO, section="general", filterable=True, sortable=True
            ),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=[
                    {"value": "released", "label": "Released"},
                    {"value": "blocked", "label": "Blocked"},
                ],
                filterable=True,
            ),
            "stock": prop(
                "embedded",
                "Stock",
                **RO,
                section="general",
                properties={
                    "available": prop("decimal", "Available", **RO),
                    "reserved": prop("decimal", "Reserved", **RO),
                },
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
            "object": "batch",
            "id": (f"bat_{r.get('id')}" if r.get("id") is not None else None),
            **r,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        return {}, {k for k in model if k != "object"}
