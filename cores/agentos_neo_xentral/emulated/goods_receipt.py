"""Xentral V3 facade · goodsReceipt — Wareneingang (docs/01-model.md §5.2).

SECOND UPSTREAM (docs/02-ist-analyse §2b): reads the BF entity API
``GET /api/entity/goodsReceipt`` (verified live: read + LIST — the list that was
missing on v1/v3, docs/05 #9 revised). The BF record carries documentNumber,
status, qualityCheckStatus and the document links (purchaseOrder, returnOrder,
parcelReceipt, businessPartner). What remains upstream-open: cancel/storno and a
line-item write model (creation still goes through the PO action).
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref, status_map

# The BF goodsReceipt status vocabulary is open | completed | closed (verified
# live against mvp) plus cancelled. Read maps upstream → model; a filter by the
# model value is translated back to the upstream vocabulary in
# ``filter_value_maps`` (F4: filtering by the model value hit the upstream
# unmapped and always returned 0).
_STATUS = {
    "open": "open",
    "completed": "posted",
    "closed": "closed",
    "cancelled": "cancelled",
    # tolerated fallbacks (older/alternate upstream spellings)
    "draft": "open",
    "booked": "posted",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("open", "posted", "closed", "cancelled")
]


class GoodsReceiptAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="GoodsReceipt",
        label_en="Goods receipt",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.goodsReceipt",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),
    )
    v3_path = "/api/entity/goodsReceipt"
    include = ""
    preview_template = "{{number}}"
    bf_sort = True
    query_aliases = {"number": "documentNumber"}
    # Model status value → upstream BF status value, so a consumer filters by the
    # model chain it sees while the upstream receives its own vocabulary (F4).
    filter_value_maps = {"status": {"posted": "completed"}}
    sections = {
        "general": {"label": "General"},
        "items": {"label": "Items"},
        "flow": {"label": "Document flow"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    self.step_cmd(
                        "post",
                        "Post",
                        wish=(True),
                    ),
                    self.step_cmd(
                        "cancel",
                        "Cancel",
                        wish=True,
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "proposeStorageLocations",
                "Propose storage locations",
                wish=True,
            ),
            self.action_def(
                "printProductLabels",
                "Print product labels",
                wish=True,
            ),
            self.action_def(
                "printBatchLabels",
                "Print batch labels",
                wish=True,
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "references": prop(
                "embedded",
                "References",
                **RO,
                section="general",
                properties={
                    "supplierDeliveryNoteNumber": prop("string", "Supplier delivery note", **RO),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                **RO,
                section="general",
                properties={
                    "received": prop("date", "Received", **RO),
                    "posted": prop("date", "Posted", **RO),
                },
            ),
            "number": prop(
                "string",
                "Number",
                **RO,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=_STATUS_OPTIONS,
                filterable=True,
                previewable=True,
            ),
            "supplier": prop(
                "reference",
                "Supplier",
                reference="Supplier",
                renderProperty="name",
                **RO,
                section="general",
                previewable=True,
            ),
            "qualityCheck": prop(
                "embedded",
                "Quality check",
                **RO,
                section="general",
                properties={"status": prop("string", "Status", **RO)},
            ),
            "items": prop(
                "collection",
                "Items",
                **RO,
                section="items",
                node={
                    "properties": {
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
                        "storageLocation": prop(
                            "reference",
                            "Storage location",
                            reference="StorageLocation",
                            renderProperty="name",
                            **RO,
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
                    "purchaseOrder": prop(
                        "reference",
                        "Purchase order",
                        reference="PurchaseOrder",
                        renderProperty="number",
                        **RO,
                    ),
                    "returnOrder": prop(
                        "reference", "Return", reference="Return", renderProperty="number", **RO
                    ),
                    "salesOrder": prop(
                        "reference",
                        "Sales order",
                        reference="SalesOrder",
                        renderProperty="number",
                        **RO,
                    ),
                    "parcelReceipt": prop("string", "Parcel receipt", **RO),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        bp = r.get("businessPartner")
        po = r.get("purchaseOrder")
        ro = r.get("returnOrder")
        so = r.get("salesOrder")
        pr = r.get("parcelReceipt")
        return {
            "object": "goodsReceipt",
            "references": {"supplierDeliveryNoteNumber": None},
            "dates": {"received": None, "posted": None},
            # BF entities are fetched by uuid (GET /{id} 404s, GET /{uuid} 200);
            # encode the uuid so the speaking id round-trips through `get` (F3).
            "id": (
                f"gr_{r['uuid']}"
                if r.get("uuid")
                else (f"gr_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "number": r.get("documentNumber"),
            "status": status_map(_STATUS, r.get("status"), "draft"),
            "supplier": ref(
                "sup_", bp.get("id") if isinstance(bp, dict) else bp, None, None, "suppliers"
            ),
            "qualityCheck": {"status": r.get("qualityCheckStatus")},
            "items": [],
            "documents": {
                "purchaseOrder": ref(
                    "po_",
                    po.get("id") if isinstance(po, dict) else po,
                    None,
                    None,
                    "purchaseOrders",
                ),
                "returnOrder": ref(
                    "ret_", ro.get("id") if isinstance(ro, dict) else ro, None, None, "returns"
                ),
                "salesOrder": ref(
                    "so_", so.get("id") if isinstance(so, dict) else so, None, None, "salesOrders"
                ),
                "parcelReceipt": (str(pr.get("id")) if isinstance(pr, dict) else pr),
            },
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # Creation goes through the PO action; direct write model + storno are open.
        return {}, {
            k
            for k in model
            if k not in {"object", "id", "number", "status", "documents", "createdAt", "updatedAt"}
        }
