from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from entity_registry.core_sdk import EmulationManifest
from .product_subresource_base import ProductSubresourceAdapterBase, property_shape, ref


class PartsListItemAdapter(ProductSubresourceAdapterBase):
    manifest = EmulationManifest(
        key="PartsListItem",
        label_en="Parts List Item",
        category="MasterData",
        rollout_batch="bom-product-parts-v1",
        adapter="v1-v2-product-parts",
        source_apis=(
            "/api/v1/products/{id}/parts",
            "/api/v2/products/{id}/parts",
        ),
        operations=("list", "create", "update", "delete"),
    )

    base_path = "/api/v1/products"
    product_child_path = "/api/v1/products/{product_id}/parts"
    create_path_template = "/api/v2/products/{product_id}/parts"
    update_path_template = "/api/v2/products/{product_id}/parts"
    query_aliases = {
        "parentProductId": "product",
        "productId": "part",
    }
    payload_read_only_fields = {"uuid", "createdAt", "updatedAt"}
    product_filter_keys = ("product",)

    def preview_template(self) -> str:
        return "{{parentProductId.id}} -> {{productId.id}} x {{quantity}}"

    def sections(self) -> dict[str, dict[str, str]]:
        return {
            "general": {"label": "General"},
            "references": {"label": "References"},
            "production": {"label": "Production"},
        }

    def properties(self) -> dict[str, Any]:
        p = property_shape
        return {
            "id": p("string", "ID", access="readOnly", filterable=True, previewable=True),
            "position": p("integer", "Position", sortable=True, section="production"),
            "productId": p(
                "reference",
                "Component product",
                reference="Product",
                renderProperty="number",
                filterable=True,
                section="references",
            ),
            "reference": p("string", "Reference", searchable=True, section="general"),
            "place": p("string", "Place", section="production"),
            "layer": p("string", "Layer", section="production"),
            "parentProductId": p(
                "reference",
                "Parent product",
                reference="Product",
                renderProperty="number",
                filterable=True,
                section="references",
            ),
            "quantity": p("decimal", "Quantity", previewable=True, section="production"),
            "value": p("string", "Value", searchable=True, section="production"),
            "formFactor": p("string", "Form factor", section="production"),
            "isAlternative": p(
                "boolean", "Alternative part", filterable=True, section="production"
            ),
            "zAxis": p("string", "Z axis", section="production"),
            "xPosition": p("string", "X position", section="production"),
            "yPosition": p("string", "Y position", section="production"),
            "type": p(
                "select",
                "Type",
                filterable=True,
                sortable=True,
                section="production",
                options=[
                    {"value": "shopping part", "label": "Shopping part"},
                    {"value": "information part / service", "label": "Information part / service"},
                    {"value": "provision", "label": "Provision"},
                ],
            ),
            "createdAt": p(
                "datetime", "Created at", access="readOnly", filterable=True, sortable=True
            ),
            "updatedAt": p(
                "datetime", "Updated at", access="readOnly", filterable=True, sortable=True
            ),
        }

    def normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        out = deepcopy(record)
        if out.get("id") is not None:
            out["id"] = str(out["id"])
        part = out.pop("part", out.pop("product", out.get("productId", None)))
        parent = out.pop("parentProduct", out.get("parentProductId", None))
        out["parentProductId"] = ref(parent)
        out["productId"] = ref(part)
        out["quantity"] = out.pop("quantity", out.get("amount"))
        out["position"] = out.pop("position", out.get("sort"))
        return {key: out.get(key) for key in self.properties() if key in out}

    def payload(self, body: bytes | None) -> list[dict[str, Any]]:
        if not body:
            return []
        data = json.loads(body.decode("utf-8"))
        rows = data if isinstance(data, list) else [data]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("PartsListItem payload must be an object or an array of objects")
        payload: list[dict[str, Any]] = []
        for row in rows:
            child: dict[str, Any] = {}
            if row.get("id") is not None:
                child["id"] = str(row["id"])
            part = row.get("productId") or row.get("part") or row.get("product")
            if part is not None:
                child["part"] = ref(part)
            if row.get("amount") is not None or row.get("quantity") is not None:
                child["amount"] = row.get("amount", row.get("quantity"))
            if row.get("type") is not None:
                child["type"] = row.get("type")
            if row.get("reference") is not None:
                child["reference"] = row.get("reference")
            parent = row.get("parentProductId") or row.get("parentProduct")
            if parent is not None:
                child["__parentProductId"] = ref(parent)
            payload.append(child)
        return payload

    def _product_id_from_body(self, body: Any) -> str | None:
        rows = body if isinstance(body, list) else [body]
        for row in rows:
            if not isinstance(row, dict):
                continue
            parent = (
                row.get("__parentProductId")
                or row.get("parentProductId")
                or row.get("parentProduct")
            )
            if isinstance(parent, dict) and parent.get("id") is not None:
                return str(parent["id"])
            if parent not in (None, ""):
                return str(parent)
        return None

    def wire_payload(self, payload: Any) -> Any:
        rows = payload if isinstance(payload, list) else [payload]
        cleaned: list[Any] = []
        for row in rows:
            if isinstance(row, dict):
                item = deepcopy(row)
                item.pop("__parentProductId", None)
                cleaned.append(item)
            else:
                cleaned.append(row)
        return cleaned
