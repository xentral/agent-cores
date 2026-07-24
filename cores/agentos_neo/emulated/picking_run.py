"""Xentral V3 facade · pickingRun — Kommissionierung (docs/01-model.md §7.6).

Reads ``GET /v1/pickLists`` (verified live, Feature-Flag Mobile-Picking: id,
documentNumber, name, status, mobilePickingStatus/ProcessType, activePickingUser,
numberOfDeliveryNotes/Products, totalWeight, warehouses). Actions start/complete +
tote assignment exist upstream (docs/05 "vorhanden — NICHT bauen"); what's missing
upstream is CREATE by criteria (wave) and granular task-level pick confirmation
(docs/05 #3) — blue wishes. Task list/progress details are not exposed on the list
payload — wish.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref, status_map

_STATUS = {
    "draft": "draft",
    "released": "released",
    "open": "released",
    "inProgress": "inProgress",
    "picking": "inProgress",
    "picked": "picked",
    "completed": "completed",
    "done": "completed",
    "cancelled": "cancelled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("draft", "released", "inProgress", "picked", "completed", "cancelled")
]


class PickingRunAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="PickingRun",
        label_en="Picking run",
        category="documents",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.pickingRun",
        source_apis=("agentos_neo",),
        operations=("list", "read"),
    )
    v3_path = "/api/v1/pickLists"
    include = ""
    preview_template = "{{number}}"
    v1_paging = True
    sections = {
        "general": {"label": "General"},
        "progress": {"label": "Progress"},
        "flow": {"label": "Document flow"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "release", "Release", wish="v1 pickLists is read-only — no state writes."
                    ),
                    self.step_cmd(
                        "start", "Start", wish="v1 pickLists is read-only — no state writes."
                    ),
                    self.step_cmd(
                        "pause", "Pause", wish="v1 pickLists is read-only — no state writes."
                    ),
                    self.step_cmd(
                        "complete", "Complete", wish="v1 pickLists is read-only — no state writes."
                    ),
                    self.step_cmd(
                        "cancel", "Cancel", wish="v1 pickLists is read-only — no state writes."
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def("assign", "Assign picker", wish="Picker assignment is not exposed."),
            self.action_def(
                "reprioritize", "Reprioritize", wish="Priority changes are not exposed."
            ),
            self.action_def(
                "printPickList", "Print pick list", wish="Pick-list print/PDF is not exposed."
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "criteria": prop(
                "embedded",
                "Criteria",
                **RO,
                section="general",
                properties={
                    "channel": prop("reference", "Channel", **RO, reference="Channel"),
                    "shippingMethod": prop(
                        "reference", "Shipping method", **RO, reference="ShippingMethod"
                    ),
                },
            ),
            "orders": prop(
                "collection",
                "Orders",
                **RO,
                section="general",
                node={
                    "properties": {
                        "id": prop("string", "ID", **RO),
                    }
                },
            ),
            "containers": prop(
                "collection",
                "Containers",
                **RO,
                section="general",
                node={
                    "properties": {
                        "name": prop("string", "Name", **RO),
                        "barcode": prop("string", "Barcode", **RO),
                    }
                },
            ),
            "number": prop(
                "string",
                "Number",
                **RO,
                section="general",
                previewable=True,
            ),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=_STATUS_OPTIONS,
                filterable=True,
                previewable=True,
            ),
            "strategy": prop(
                "select",
                "Strategy",
                **RO,
                section="general",
                options=[
                    {"value": v, "label": v.capitalize()}
                    for v in ("single", "multiOrder", "wave", "zone")
                ],
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="general",
            ),
            "assignedTo": prop(
                "collection",
                "Assigned to",
                **RO,
                section="general",
                node={"properties": {"name": prop("string", "Name", **RO)}},
            ),
            "progress": prop(
                "embedded",
                "Progress",
                **RO,
                section="progress",
                properties={
                    "deliveryNotes": prop("integer", "Delivery notes", **RO),
                    "products": prop("integer", "Products", **RO),
                    "totalWeight": prop("decimal", "Total weight", **RO),
                },
            ),
            "tasks": prop(
                "collection",
                "Tasks",
                **RO,
                section="progress",
                node={
                    "properties": {
                        "status": prop("select", "Status", **RO),
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
                        "quantity": prop(
                            "embedded",
                            "Quantity",
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
                    "deliveryNotes": prop(
                        "collection",
                        "Delivery notes",
                        **RO,
                        node={"properties": {"id": prop("string", "ID", **RO)}},
                    )
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        user = r.get("activePickingUser")
        whs = r.get("warehouses")
        wh0 = whs[0] if isinstance(whs, list) and whs else None
        return {
            "object": "pickingRun",
            "criteria": None,
            "orders": None,
            "containers": None,
            "id": (f"pkr_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "name": r.get("name"),
            "status": status_map(_STATUS, r.get("mobilePickingStatus") or r.get("status"), "draft"),
            "strategy": r.get("mobilePickingProcessType"),
            "warehouse": ref(
                "wh_",
                wh0.get("id") if isinstance(wh0, dict) else wh0,
                None,
                wh0.get("name") if isinstance(wh0, dict) else None,
                "warehouses",
            ),
            "assignedTo": (
                [{"name": user.get("name") if isinstance(user, dict) else str(user)}]
                if user
                else []
            ),
            "progress": {
                "deliveryNotes": r.get("numberOfDeliveryNotes"),
                "products": r.get("numberOfProducts"),
                "totalWeight": r.get("totalWeight"),
            },
            "tasks": [],
            "documents": {"deliveryNotes": []},
            "createdAt": None,
            "updatedAt": None,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # Create-by-criteria has no upstream endpoint (docs/05 #3).
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
