from __future__ import annotations

from copy import deepcopy
from typing import Any

from entity_registry.core_sdk import EmulationManifest
from .product_subresource_base import ProductSubresourceAdapterBase, money, property_shape, ref


class PurchasePriceAdapter(ProductSubresourceAdapterBase):
    manifest = EmulationManifest(
        key="PurchasePrice",
        label_en="Purchase Price",
        category="Purchasing",
        rollout_batch="purchase-price-v2",
        adapter="v2-purchase-price",
        source_apis=("/api/v2/purchasePrices", "/api/v1/products/{id}/purchasePrices"),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v2/purchasePrices"
    read_path_template = "/api/v1/purchasePrices/{handle}"
    delete_path_template = "/api/v1/purchasePrices/{handle}"
    product_child_path = "/api/v1/products/{product_id}/purchasePrices"
    query_aliases = {"productId": "product", "supplierId": "supplier"}

    def preview_template(self) -> str:
        return "{{product.id}} · {{price.amount}} {{price.currency}}"

    def properties(self) -> dict[str, Any]:
        p = property_shape
        return {
            "id": p("string", "ID", access="readOnly", filterable=True, previewable=True),
            "product": p(
                "reference",
                "Product",
                reference="Product",
                renderProperty="number",
                filterable=True,
                section="references",
            ),
            "productId": p(
                "reference",
                "Product",
                reference="Product",
                renderProperty="number",
                section="references",
            ),
            "supplier": p(
                "reference",
                "Supplier",
                reference="Supplier",
                renderProperty="name",
                filterable=True,
                section="references",
            ),
            "supplierId": p(
                "reference",
                "Supplier",
                reference="Supplier",
                renderProperty="name",
                section="references",
            ),
            "objectType": p("string", "Object type", access="readOnly", section="general"),
            "projectId": p(
                "reference", "Project", reference="Project", filterable=True, section="references"
            ),
            "isStandardSupplier": p("boolean", "Standard supplier", section="references"),
            "isDeleted": p("boolean", "Deleted", filterable=True, section="general"),
            "supplierDesignation": p(
                "string", "Supplier designation", searchable=True, section="general"
            ),
            "supplierItemNumber": p(
                "string", "Supplier item number", searchable=True, section="general"
            ),
            "fromQuantity": p("decimal", "From quantity", filterable=True, section="pricing"),
            "packageAmount": p("decimal", "Package amount", section="pricing"),
            "price": p(
                "embedded",
                "Price",
                previewable=True,
                section="pricing",
                properties={"amount": p("decimal", "Amount"), "currency": p("string", "Currency")},
            ),
            "priceAmount": p("decimal", "Price amount", section="pricing"),
            "currency": p("string", "Currency", filterable=True, section="pricing"),
            "standardDeliveryTime": p("integer", "Standard delivery time", section="general"),
            "standardDeliveryTimeUnit": p(
                "select",
                "Standard delivery time unit",
                section="general",
                options=[
                    {"value": "days", "label": "Days"},
                    {"value": "weeks", "label": "Weeks"},
                ],
            ),
            "currentDeliveryTime": p("integer", "Current delivery time", section="general"),
            "currentDeliveryTimeUnit": p(
                "select",
                "Current delivery time unit",
                section="general",
                options=[
                    {"value": "days", "label": "Days"},
                    {"value": "weeks", "label": "Weeks"},
                ],
            ),
            "supplierStockLevel": p("integer", "Supplier stock level", section="general"),
            "supplierStockDate": p(
                "date", "Supplier stock date", filterable=True, section="general"
            ),
            "safetyStockLevel": p("integer", "Safety stock level", section="general"),
            "internalComment": p("string", "Internal comment", section="general"),
            "editor": p("string", "Editor", access="readOnly", section="general"),
            "loggedAt": p(
                "datetime", "Logged at", access="readOnly", filterable=True, sortable=True
            ),
            "companyId": p("integer", "Company ID", section="general"),
            "isChangedViaApi": p(
                "boolean", "Changed via API", access="readOnly", section="general"
            ),
            "isFrameworkAgreement": p(
                "boolean", "Framework agreement", filterable=True, section="pricing"
            ),
            "frameworkAgreementValidFrom": p(
                "date", "Framework agreement valid from", filterable=True, section="validity"
            ),
            "frameworkAgreementValidUntil": p(
                "date", "Framework agreement valid until", filterable=True, section="validity"
            ),
            "frameworkAgreementQuantity": p(
                "integer", "Framework agreement quantity", section="pricing"
            ),
            "isNotCalculated": p("boolean", "Not calculated", section="pricing"),
            "validFrom": p("date", "Valid from", filterable=True, section="validity"),
            "expiresAt": p("date", "Expires at", section="validity"),
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
        out["product"] = ref(out.pop("product", out.get("productId")))
        out["productId"] = ref(out.get("product"))
        out["supplier"] = ref(out.pop("supplier", out.get("supplierId")))
        out["supplierId"] = ref(out.get("supplier"))
        price = money(out.get("price"), out.get("currency"))
        out["price"] = price
        if price:
            out["priceAmount"] = price.get("amount")
            out["currency"] = price.get("currency")
        return {key: out.get(key) for key in self.properties() if key in out}

    def payload(self, body: bytes | None) -> dict[str, Any]:
        data = super().payload(body)
        if not isinstance(data, dict):
            return {}
        if "productId" in data and "product" not in data:
            data["product"] = ref(data.pop("productId"))
        if "supplierId" in data and "supplier" not in data:
            data["supplier"] = ref(data.pop("supplierId"))
        if "priceAmount" in data and "price" not in data:
            data["price"] = money(data.pop("priceAmount"), data.get("currency"))
        return data
