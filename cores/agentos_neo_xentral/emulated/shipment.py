"""Xentral V3 facade · shipment — Versandeinheit (docs/01-model.md §7.1).

Reads ``GET /v1/shipments`` (verified live: id, shippingMethod, deliveryNote{id},
salesOrder{id}, tracking{number, link, carrier}, label, sentAt/-Timestamp,
additionalPackages). Label creation exists upstream (``POST /v1/shipments`` +
createLabel per delivery note, docs/03) but is not wired here yet — writes are
blue wishes until that orchestration is built and proven.

Same v1 pagination contract as salesPrices: page[number] AND page[size] (10..50)
are both required on lists.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref


class ShipmentAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Shipment",
        label_en="Shipment",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.shipment",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),
    )
    v3_path = "/api/v1/shipments"
    include = ""
    preview_template = "{{tracking.number}}"
    v1_paging = True
    sections = {
        "general": {"label": "General"},
        "tracking": {"label": "Tracking"},
        "flow": {"label": "Document flow"},
    }

    def actions(self):
        return [
            self.action_def("createLabel", "Create label", wish=True),
            self.action_def("cancelLabel", "Cancel label", wish=True),
            self.action_def("downloadLabel", "Download label", wish=True),
            self.action_def(
                "refreshTracking",
                "Refresh tracking",
                wish=True,
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
                    {"value": v, "label": v}
                    for v in (
                        "label",
                        "handedOver",
                        "inTransit",
                        "delivered",
                        "exception",
                        "returned",
                    )
                ],
            ),
            "carrier": prop("string", "Carrier", **RO, section="general"),
            "trackingNumber": prop("string", "Tracking number", **RO, section="tracking"),
            "trackingUrl": prop("string", "Tracking URL", **RO, section="tracking"),
            "labelUrl": prop("string", "Label URL", **RO, section="tracking"),
            "weight": prop(
                "embedded",
                "Weight",
                **RO,
                section="general",
                properties={
                    "value": prop("number", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
            "packages": prop(
                "collection",
                "Packages",
                **RO,
                section="tracking",
                node={
                    "properties": {
                        "trackingNumber": prop("string", "Tracking number", **RO),
                    }
                },
            ),
            "events": prop(
                "collection",
                "Events",
                **RO,
                section="tracking",
                node={
                    "properties": {
                        "at": prop("datetime", "At", **RO),
                        "status": prop("string", "Status", **RO),
                        "location": prop("string", "Location", **RO),
                    }
                },
            ),
            "number": prop("string", "Number", **RO, section="general", previewable=True),
            "shippingMethod": prop(
                "reference",
                "Shipping method",
                reference="ShippingMethod",
                renderProperty="name",
                section="general",
                previewable=True,
            ),
            "tracking": prop(
                "embedded",
                "Tracking",
                **RO,
                section="tracking",
                properties={
                    "number": prop("string", "Tracking number", **RO),
                    "link": prop("string", "Tracking link", **RO),
                    "carrier": prop("string", "Carrier", **RO),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                **RO,
                section="general",
                properties={"sentAt": prop("date", "Sent at", **RO)},
            ),
            "additionalPackages": prop(
                "collection",
                "Additional packages",
                **RO,
                section="tracking",
                node={"properties": {"trackingNumber": prop("string", "Tracking number", **RO)}},
            ),
            "documents": prop(
                "embedded",
                "Documents",
                **RO,
                section="flow",
                properties={
                    "deliveryNote": prop(
                        "reference",
                        "Delivery note",
                        reference="DeliveryNote",
                        renderProperty="number",
                        **RO,
                    ),
                    "salesOrder": prop(
                        "reference",
                        "Sales order",
                        reference="SalesOrder",
                        renderProperty="number",
                        **RO,
                    ),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        tr = r.get("tracking") or {}
        dn = r.get("deliveryNote")
        so = r.get("salesOrder")
        sm = r.get("shippingMethod")
        return {
            "object": "shipment",
            "status": None,
            "carrier": tr.get("carrier"),
            "trackingNumber": tr.get("number"),
            "trackingUrl": tr.get("link") or None,
            "labelUrl": r.get("label"),
            "weight": None,
            "packages": [
                {"trackingNumber": p.get("trackingNumber") or p.get("number")}
                for p in (r.get("additionalPackages") or [])
                if isinstance(p, dict)
            ],
            "events": None,
            "id": (f"shp_{r.get('id')}" if r.get("id") is not None else None),
            "number": tr.get("number"),
            "shippingMethod": ref(
                "ship_",
                sm.get("id") if isinstance(sm, dict) else sm,
                None,
                sm.get("name") if isinstance(sm, dict) else None,
                "shippingMethods",
            ),
            "tracking": {
                "number": tr.get("number"),
                "link": tr.get("link") or None,
                "carrier": tr.get("carrier"),
            },
            "dates": {"sentAt": r.get("sentAt")},
            "additionalPackages": [
                {"trackingNumber": p.get("trackingNumber") or p.get("number")}
                for p in (r.get("additionalPackages") or [])
                if isinstance(p, dict)
            ],
            "documents": {
                "deliveryNote": ref(
                    "dn_", dn.get("id") if isinstance(dn, dict) else dn, None, None, "deliveryNotes"
                ),
                "salesOrder": ref(
                    "so_", so.get("id") if isinstance(so, dict) else so, None, None, "salesOrders"
                ),
            },
            "createdAt": r.get("sentAtTimestamp"),
            "updatedAt": None,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # Label/shipment creation exists upstream but is not orchestrated here yet.
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
