from __future__ import annotations

from copy import deepcopy
from typing import Any

from entity_registry.core_sdk import EmulationManifest
from .product_subresource_base import ProductSubresourceAdapterBase, money, property_shape, ref


class SalesPriceAdapter(ProductSubresourceAdapterBase):
    manifest = EmulationManifest(
        key="SalesPrice",
        label_en="Sales Price",
        category="Sales",
        rollout_batch="sales-price-v3",
        adapter="v3-sales-price",
        source_apis=("/api/v3/salesPrices", "/api/v1/products/{id}/salesPrices"),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v3/salesPrices"
    product_child_path = "/api/v1/products/{product_id}/salesPrices"
    query_aliases = {
        "productId": "product",
        "customerId": "customer",
        "businessPartnerGroupId": "groupId",
    }

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
            "customer": p(
                "reference",
                "Customer",
                reference="Customer",
                renderProperty="name",
                filterable=True,
                section="references",
            ),
            "customerId": p(
                "reference",
                "Customer",
                reference="Customer",
                renderProperty="name",
                section="references",
            ),
            "customerGroup": p(
                "reference",
                "Customer group",
                reference="BusinessPartnerGroup",
                renderProperty="name",
                section="references",
            ),
            "businessPartnerGroupId": p(
                "reference",
                "Business partner group",
                reference="BusinessPartnerGroup",
                filterable=True,
                section="references",
            ),
            "object": p("string", "Object", section="general"),
            "projectId": p(
                "reference", "Project", reference="Project", filterable=True, section="references"
            ),
            "amount": p("decimal", "Amount", filterable=True, sortable=True, section="pricing"),
            "quantity": p("decimal", "Quantity", section="pricing"),
            "packagingUnit": p("string", "Packaging unit", section="pricing"),
            "packageAmount": p("decimal", "Package amount", section="pricing"),
            "createdDate": p(
                "date",
                "Created date",
                access="readOnly",
                filterable=True,
                sortable=True,
                section="validity",
            ),
            "price": p(
                "embedded",
                "Price",
                previewable=True,
                section="pricing",
                properties={"amount": p("decimal", "Amount"), "currency": p("string", "Currency")},
            ),
            "priceAmount": p("decimal", "Price amount", section="pricing"),
            "currency": p("string", "Currency", section="pricing"),
            "customerProductNumber": p(
                "string", "Customer product number", searchable=True, section="general"
            ),
            "applicability": p(
                "select",
                "Applicability",
                filterable=True,
                section="pricing",
                options=[
                    {"value": "Kunde", "label": "Customer"},
                    {"value": "Gruppe", "label": "Group"},
                ],
            ),
            "isNotCalculatedFromExchangeTable": p(
                "boolean", "Not calculated from exchange table", section="pricing"
            ),
            "shouldHideInBulkPriceListing": p(
                "boolean", "Hide in bulk price listing", section="pricing"
            ),
            "remark": p("string", "Remark", section="general"),
            "editor": p("string", "Editor", access="readOnly", section="general"),
            "loggedAt": p("datetime", "Logged at", access="readOnly", section="general"),
            "companyId": p("integer", "Company ID", section="general"),
            "isDeleted": p("boolean", "Deleted", filterable=True, section="general"),
            "isApiChange": p("boolean", "API change", access="readOnly", section="general"),
            "validFrom": p("date", "Valid from", filterable=True, section="validity"),
            "expiresAt": p("date", "Expires at", section="validity"),
            "exchangeRate": p("decimal", "Exchange rate", access="readOnly", section="pricing"),
            "exchangeRateDate": p(
                "date", "Exchange rate date", access="readOnly", section="pricing"
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
        out["product"] = ref(out.pop("product", out.get("productId")))
        out["productId"] = ref(out.get("product"))
        out["customer"] = ref(out.pop("customer", out.get("customerId")))
        out["customerId"] = ref(out.get("customer"))
        out["customerGroup"] = ref(out.get("customerGroup"))
        out["businessPartnerGroupId"] = ref(
            out.get("businessPartnerGroupId", out.get("customerGroup"))
        )
        price = money(out.get("price"), out.get("currency"))
        out["price"] = price
        if price:
            out["priceAmount"] = price.get("amount")
            out["currency"] = price.get("currency")
        out["amount"] = out.get("amount", out.get("quantity"))
        out["shouldHideInBulkPriceListing"] = out.pop(
            "shouldHideInBulkPriceListing",
            out.pop("hideInBulkPriceListing", None),
        )
        return {key: out.get(key) for key in self.properties() if key in out}

    def payload(self, body: bytes | None) -> dict[str, Any]:
        data = super().payload(body)
        if not isinstance(data, dict):
            return {}
        if "productId" in data and "product" not in data:
            data["product"] = ref(data.pop("productId"))
        if "customerId" in data and "customer" not in data:
            data["customer"] = ref(data.pop("customerId"))
        if "businessPartnerGroupId" in data and "customerGroup" not in data:
            data["customerGroup"] = ref(data.pop("businessPartnerGroupId"))
        if "priceAmount" in data and "price" not in data:
            data["price"] = money(data.pop("priceAmount"), data.get("currency"))
        if "amount" in data and "quantity" not in data:
            data["quantity"] = data.pop("amount")
        if "shouldHideInBulkPriceListing" in data and "hideInBulkPriceListing" not in data:
            data["hideInBulkPriceListing"] = data.pop("shouldHideInBulkPriceListing")
        return data
