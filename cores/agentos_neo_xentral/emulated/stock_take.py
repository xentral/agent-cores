"""Xentral V3 facade · stockTake — Inventur (docs/01-model.md §7.7, GoBD).

Reads ``GET /v1/inventoryRuns`` (verified live: id, name, status, warehouse,
storageLocations, controller, counter, createdAt/updatedAt) — docs/03 calls this
"gute Basis". The target model's positions (expected/counted/difference with
values) live in the counting-list reports and are not composed yet — blue wish.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref, status_map

_STATUS = {
    "draft": "draft",
    "open": "counting",
    "counting": "counting",
    "inProgress": "counting",
    "review": "review",
    "completed": "posted",
    "posted": "posted",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("draft", "counting", "review", "posted", "cancelled")
]


class StockTakeAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StockTake",
        label_en="Stock take",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.stockTake",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),
    )
    v3_path = "/api/v1/inventoryRuns"
    include = ""
    preview_template = "{{name}}"
    v1_paging = True
    sections = {
        "general": {"label": "General"},
        "positions": {"label": "Positions"},
        "flow": {"label": "Document flow"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "startCounting",
                        "Start counting",
                        wish="v1 inventoryRuns is read-only — no state writes.",
                    ),
                    self.step_cmd(
                        "submit", "Submit", wish="v1 inventoryRuns is read-only — no state writes."
                    ),
                    self.step_cmd(
                        "post", "Post", wish="v1 inventoryRuns is read-only — no state writes."
                    ),
                    self.step_cmd(
                        "cancel", "Cancel", wish="v1 inventoryRuns is read-only — no state writes."
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "exportCountingList",
                "Export counting list",
                wish="Counting-list export is not exposed.",
            ),
            self.action_def("recount", "Recount", wish="Recount has no public trigger."),
            self.action_def(
                "addPosition",
                "Add position",
                wish="Adding positions is not exposed via v1 inventoryRuns.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "dates": prop(
                "embedded",
                "Dates",
                **RO,
                section="general",
                properties={
                    "keyDate": prop("date", "Key date", **RO),
                    "started": prop("datetime", "Started", **RO),
                    "posted": prop("datetime", "Posted", **RO),
                },
            ),
            "totals": prop(
                "embedded",
                "Totals",
                **RO,
                section="general",
                properties={
                    "positions": prop("integer", "Positions", **RO),
                    "differences": prop("integer", "Differences", **RO),
                },
            ),
            "name": prop(
                "string",
                "Name",
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
                options=_STATUS_OPTIONS,
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
            "scope": prop(
                "embedded",
                "Scope",
                **RO,
                section="general",
                properties={"storageLocations": prop("string", "Storage locations", **RO)},
            ),
            "people": prop(
                "embedded",
                "People",
                **RO,
                section="general",
                properties={
                    "controller": prop("string", "Controller", **RO),
                    "counter": prop("string", "Counter", **RO),
                },
            ),
            "positions": prop(
                "collection",
                "Positions",
                **RO,
                section="positions",
                node={
                    "properties": {
                        "storageLocation": prop(
                            "reference",
                            "Storage location",
                            reference="StorageLocation",
                            renderProperty="name",
                            **RO,
                        ),
                        "product": prop(
                            "reference", "Product", reference="Product", renderProperty="name", **RO
                        ),
                        "expected": prop(
                            "embedded",
                            "Expected",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                        "counted": prop(
                            "embedded",
                            "Counted",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                        "difference": prop(
                            "embedded",
                            "Difference",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                    }
                },
            ),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "stockMovements": prop(
                        "collection",
                        "Stock movements",
                        **RO,
                        node={"properties": {"id": prop("string", "ID", **RO)}},
                    )
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        wh = r.get("warehouse")
        locs = r.get("storageLocations")
        ctrl = r.get("controller")
        cnt = r.get("counter")

        def person(v: Any) -> Any:
            if isinstance(v, dict):
                return v.get("name") or v.get("id")
            return v

        return {
            "object": "stockTake",
            "dates": {"keyDate": None, "started": r.get("createdAt"), "posted": None},
            "totals": {"positions": None, "differences": None},
            "id": (f"stk_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "warehouse": ref(
                "wh_",
                wh.get("id") if isinstance(wh, dict) else wh,
                None,
                wh.get("name") if isinstance(wh, dict) else None,
                "warehouses",
            ),
            "scope": {
                "storageLocations": (
                    str(len(locs)) + " locations" if isinstance(locs, list) else "all"
                )
            },
            "people": {"controller": person(ctrl), "counter": person(cnt)},
            "positions": [],
            "documents": {"stockMovements": []},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # v1 inventory endpoints exist upstream; write orchestration not built yet.
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
