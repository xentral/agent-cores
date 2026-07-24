"""xentral_api · Product — v3 read-only master-data adapter.

Rebuilt against the final Xentral v3 products read API (xentral/xentral PR
#24325): ``GET /api/v3/products`` (list) and ``GET /api/v3/products/{id}``
(show), scope ``product:read``. That endpoint replaces the 13+ v1/v2 calls a
complete product used to need — it returns the base fields in one request and
embeds any of 30 sub-resources on demand via ``?include=``.

The endpoint is **read-only by design**, so this entity is honestly read-only:
``operations = (list, read)`` only, no create/update/delete, no actions. Every
schema field carries ``access:"readOnly"``. The writable sub-resources still
live as their own entities (``SalesPrice``, ``PurchasePrice``,
``StorageLocation``, ``PartsListItem``); the includes here are read-only
projections beside them.

Contract translation (incoming business-entity contract → v3 wire):
- pagination ``page[number]`` / ``page[size]`` → v3 ``page`` / ``perPage``
  (+ ``X-Pagination: table`` header so list meta carries total/lastPage);
- filters arrive as ``filter[i][key|op|value]`` — v3's exact shape; only the
  key is aliased (``project`` → ``project.id``) and a missing ``op`` defaults
  to ``equals``;
- sort is the flat ``sort=[-]field`` the workspace table sends — v3's exact
  shape; only the field name is aliased.

Field spec grounded in the PR's ``ProductResource`` + per-include ``*Resource``
classes (see docs and the per-include tables below).
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from ._search import extract_search, fan_out_search


_TIMEOUT_SECONDS = 60.0

# All 33 v3 include names: the 30 sub-resource collections plus the three
# base-field enrichments (project / standardSupplier / merchandiseGroup, which
# are id-only by default and become full objects when included). Requested in
# full on a detail read — the whole point of the endpoint is one complete
# product per request.
_ALL_INCLUDES: tuple[str, ...] = (
    "project",
    "standardSupplier",
    "merchandiseGroup",
    "salesPrices",
    "purchasePrices",
    "texts",
    "categories",
    "externalReferences",
    "deliveryThresholds",
    "calculationItems",
    "parts",
    "usedIn",
    "batches",
    "serialNumbers",
    "bestBeforeDates",
    "packagingUnits",
    "warehouseMinimums",
    "reservations",
    "storageLocations",
    "crossSelling",
    "rawMaterials",
    "freeFields",
    "commissions",
    "certificates",
    "workInstructions",
    "functionProtocols",
    "variants",
    "tags",
    "stock",
    "properties",
    "options",
    "salesChannels",
    "media",
)

# Filter operators the v3 products list endpoint accepts, by field — grounded in
# ProductController's filter definitions (PR #24325). Surfaced per filterable
# field as ``operators`` so a caller (and the self-check) pick a valid operator
# instead of guessing a rejected one.
_STRING_FILTER_OPS = [
    "equals",
    "notEquals",
    "in",
    "notIn",
    "contains",
    "notContains",
    "startsWith",
    "endsWith",
]
_ID_FILTER_OPS = ["equals", "notEquals", "in", "notIn"]
_BOOL_FILTER_OPS = ["equals", "notEquals"]
_DATE_FILTER_OPS = [
    "equals",
    "notEquals",
    "lessThan",
    "lessThanOrEquals",
    "greaterThan",
    "greaterThanOrEquals",
    "isNull",
    "isNotNull",
]
_FILTER_OPS_BY_FIELD: dict[str, list[str]] = {
    "number": _STRING_FILTER_OPS,
    "name": _STRING_FILTER_OPS,
    "ean": _STRING_FILTER_OPS,
    "project": _ID_FILTER_OPS,
    "isVariant": _BOOL_FILTER_OPS,
    "isMatrixProduct": _BOOL_FILTER_OPS,
    "updatedAt": _DATE_FILTER_OPS,
}


# Filter keys (resolved / wire names) whose value the v3 endpoint validates as a
# full datetime (``Y-m-d\TH:i:sP`` or ``Y-m-d H:i:s``) and rejects as a bare
# date. A caller (and the self-check) naturally sends ``YYYY-MM-DD``; expand it
# to start-of-day so the friendly form keeps working.
_DATETIME_FILTER_KEYS = {"updatedAt"}
_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _p(type_: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "label": label, **extra}


def _money(label: str) -> dict[str, Any]:
    return _p(
        "embedded",
        label,
        properties={
            "amount": _p("decimal", "Amount"),
            "currency": _p("string", "Currency"),
        },
    )


def _coll(label: str, section: str, props: dict[str, Any]) -> dict[str, Any]:
    return _p("collection", label, section=section, node={"properties": props})


def _ref(label: str, reference: str, **extra: Any) -> dict[str, Any]:
    extra.setdefault("renderProperty", "name")
    return _p("reference", label, reference=reference, **extra)


# ── include sub-node property tables (one per v3 *Resource) ────────────────────
def _sales_prices_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "product": _ref("Product", "Product"),
        "customer": _ref("Customer", "Customer"),
        "customerGroup": _ref("Customer group", "BusinessPartnerGroup"),
        "quantity": _p("decimal", "From quantity"),
        "packageAmount": _p("decimal", "Package amount"),
        "price": _money("Price"),
        "customerProductNumber": _p("string", "Customer product number"),
        "isNotCalculatedFromExchangeTable": _p("boolean", "Not calculated from exchange table"),
        "hideInBulkPriceListing": _p("boolean", "Hide in bulk price listing"),
        "remark": _p("string", "Remark"),
        "validFrom": _p("date", "Valid from"),
        "expiresAt": _p("date", "Expires at"),
    }


def _purchase_prices_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "product": _ref("Product", "Product"),
        "supplier": _ref("Supplier", "Supplier"),
        "fromQuantity": _p("decimal", "From quantity"),
        "price": _money("Price"),
        "isStandardSupplier": _p("boolean", "Standard supplier"),
        "remark": _p("string", "Remark"),
        "validUntil": _p("date", "Valid until"),
    }


def _texts_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "language": _p("string", "Language"),
        "shop": _ref("Shop", "SalesChannel"),
        "name": _p("string", "Name"),
        "shortDescription": _p("string", "Short description"),
        "description": _p("string", "Description"),
        "shopDescription": _p("string", "Shop description"),
        "metaTitle": _p("string", "Meta title"),
        "metaDescription": _p("string", "Meta description"),
        "metaKeywords": _p("string", "Meta keywords"),
        "inCatalog": _p("boolean", "In catalog"),
        "catalogName": _p("string", "Catalog name"),
        "catalogText": _p("string", "Catalog text"),
        "isActive": _p("boolean", "Active"),
    }


def _media_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "category": _p("string", "Category"),
        "title": _p("string", "Title"),
        "description": _p("string", "Description"),
        "fileId": _p("string", "File ID"),
    }


def _properties_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "type": _p("string", "Type"),
        "values": _coll(
            "Values",
            "content",
            {
                "id": _p("string", "ID"),
                "name": _p("string", "Name"),
                "value": _p("string", "Value"),
                "unit": _p("string", "Unit"),
            },
        ),
    }


def _options_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "product": _ref("Product", "Product"),
        "values": _coll(
            "Values",
            "content",
            {
                "id": _p("string", "ID"),
                "name": _p("string", "Name"),
                "priceSurcharge": _p("decimal", "Price surcharge"),
                "priceType": _p("string", "Price type"),
            },
        ),
    }


def _free_fields_props() -> dict[str, Any]:
    return {
        "id": _p("string", "Number"),
        "name": _p("string", "Label"),
        "value": _p("string", "Value"),
        "translations": _coll(
            "Translations",
            "content",
            {
                "language": _p(
                    "embedded", "Language", properties={"iso2": _p("string", "ISO code")}
                ),
                "name": _p("string", "Label"),
                "value": _p("string", "Value"),
            },
        ),
    }


def _tags_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "color": _p("string", "Color"),
    }


def _categories_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "parent": _ref("Parent", "ProductCategory"),
    }


def _variants_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "number": _p("string", "Number"),
        "ean": _p("string", "EAN"),
    }


def _external_references_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "number": _p("string", "External number"),
        "isActive": _p("boolean", "Active"),
        "isScannable": _p("boolean", "Scannable"),
        "salesChannel": _ref("Sales channel", "SalesChannel"),
    }


def _sales_channels_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "product": _ref("Product", "Product"),
        "salesChannel": _ref("Sales channel", "SalesChannel"),
        "isActive": _p("boolean", "Active"),
        "useIndividualSalesChannelSettings": _p("boolean", "Individual settings"),
        "isStockNumberSyncActive": _p("boolean", "Stock sync active"),
        "isRemainingQuantity": _p("boolean", "Remaining quantity"),
        "suggestedRetailPrice": _p("string", "Suggested retail price"),
        "suggestedStockQuantity": _p("string", "Suggested stock quantity"),
        "deliveryTime": _p("string", "Delivery time"),
        "createNewProductDuringImport": _p("boolean", "Create new product on import"),
    }


def _bom_item_props() -> dict[str, Any]:
    # Shared by `parts` (components of this product) and `usedIn` (where this
    # product is a component).
    return {
        "id": _p("string", "ID"),
        "quantity": _p("decimal", "Quantity"),
        "component": _ref("Component", "Product"),
        "parent": _ref("Parent product", "Product"),
    }


def _raw_materials_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "rawMaterial": _ref("Raw material", "Product"),
        "quantity": _p("decimal", "Quantity"),
        "useStockValue": _p("boolean", "Use stock value"),
        "sortOrder": _p("integer", "Sort order"),
        "reference": _p("string", "Reference"),
        "type": _p("string", "Type"),
    }


def _delivery_thresholds_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "recipientCountry": _p("string", "Recipient country"),
        "taxRate": _p("decimal", "Tax rate"),
        "comment": _p("string", "Comment"),
        "revenueAccount": _p("string", "Revenue account"),
        "active": _p("boolean", "Active"),
    }


def _calculation_items_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "designation": _p("string", "Designation"),
        "date": _p("date", "Date"),
        "costType": _p("string", "Cost type"),
        "includeInCalculation": _p("boolean", "Include in calculation"),
        "totalCost": _money("Total cost"),
        "forQuantity": _p("integer", "For quantity"),
        "costPerUnit": _money("Cost per unit"),
        "purchaseOrder": _ref("Purchase order", "PurchaseOrder", renderProperty="documentNumber"),
        "archived": _p("boolean", "Archived"),
        "editor": _p(
            "embedded",
            "Editor",
            properties={"id": _p("string", "ID"), "name": _p("string", "Name")},
        ),
        "internalComment": _p("string", "Internal comment"),
    }


def _commissions_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "commissionPercent": _p("decimal", "Commission percent"),
        "commissionType": _p("string", "Commission type"),
        "validFrom": _p("date", "Valid from"),
        "validTo": _p("date", "Valid to"),
        "category": _ref("Category", "ProductCategory"),
        "address": _ref("Address", "BusinessPartner"),
        "customer": _ref("Customer", "Customer"),
        "project": _ref("Project", "Project"),
    }


def _certificates_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "descriptionDe": _p("string", "Description DE"),
        "descriptionEn": _p("string", "Description EN"),
        "orderNoteDe": _p("string", "Order note DE"),
        "orderNoteEn": _p("string", "Order note EN"),
        "internalNote": _p("string", "Internal note"),
        "signature": _p("boolean", "Signature"),
        "dateOfSaleStamp": _p("boolean", "Date-of-sale stamp"),
        "priceFactor": _p("decimal", "Price factor"),
        "usdRate": _p("decimal", "USD rate"),
        "type": _p("integer", "Type"),
        "typeText": _p("string", "Type text"),
        "customerAddress": _ref("Customer address", "BusinessPartner"),
        "senderAddress": _p("string", "Sender address"),
        "layout": _ref("Layout", "Layout"),
        "certificatesEnabled": _p("boolean", "Certificates enabled"),
        "priceEur": _p("string", "Price EUR"),
        "priceUsd": _p("string", "Price USD"),
        "priceEurRetail": _p("string", "Price EUR retail"),
        "createdDate": _p("date", "Created date"),
        "file": _ref("File", "File"),
    }


def _work_instructions_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "description": _p("string", "Description"),
        "hasImage": _p("boolean", "Has image"),
        "unitTime": _p("integer", "Unit time (s)"),
        "workstationGroup": _ref("Workstation group", "WorkstationGroup"),
        "sortOrder": _p("integer", "Sort order"),
    }


def _function_protocols_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "name": _p("string", "Name"),
        "description": _p("string", "Description"),
        "hasImage": _p("boolean", "Has image"),
        "type": _p("string", "Type"),
        "continueOnError": _p("boolean", "Continue on error"),
        "sortOrder": _p("integer", "Sort order"),
    }


def _cross_selling_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "product": _ref("Product", "Product"),
        "type": _p("integer", "Type"),
        "active": _p("boolean", "Active"),
        "assignToEachOther": _p("boolean", "Assign to each other"),
        "sortOrder": _p("integer", "Sort order"),
        "note": _p("string", "Note"),
    }


def _stock_props() -> dict[str, Any]:
    # Aggregate on-hand total; emitted by v3 as a single-row collection.
    return {
        "id": _p("string", "ID"),
        "quantity": _p("decimal", "Quantity"),
    }


def _storage_locations_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "storageLocation": _ref("Storage location", "StorageLocation"),
        "quantity": _p("decimal", "Quantity"),
    }


def _reservations_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "product": _ref("Product", "Product"),
        "quantity": _p("decimal", "Quantity"),
        "reason": _p("string", "Reason"),
        "document": _p(
            "embedded",
            "Source document",
            properties={"type": _p("string", "Type"), "id": _p("string", "ID")},
        ),
    }


def _batches_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "batchNumber": _p("string", "Batch number"),
        "date": _p("date", "Date"),
        "quantity": _p("decimal", "Quantity"),
        "storageLocation": _ref("Storage location", "StorageLocation"),
        "internalNote": _p("string", "Internal note"),
    }


def _serial_numbers_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "serialNumber": _p("string", "Serial number"),
        "storageLocation": _ref("Storage location", "StorageLocation"),
        "batchNumber": _p("string", "Batch number"),
        "bestBeforeDate": _p("date", "Best-before date"),
        "internalNote": _p("string", "Internal note"),
    }


def _best_before_dates_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "bestBeforeDate": _p("date", "Best-before date"),
        "quantity": _p("decimal", "Quantity"),
        "storageLocation": _ref("Storage location", "StorageLocation"),
        "batchNumber": _p("string", "Batch number"),
        "internalNote": _p("string", "Internal note"),
    }


def _warehouse_minimums_props() -> dict[str, Any]:
    return {
        "id": _p("string", "ID"),
        "storageLocation": _ref("Storage location", "StorageLocation"),
        "minQuantity": _p("decimal", "Minimum quantity"),
        "maxQuantity": _p("decimal", "Maximum quantity"),
        "validFrom": _p("date", "Valid from"),
        "validUntil": _p("date", "Valid until"),
    }


def _packaging_units_props() -> dict[str, Any]:
    outer = {
        "quantity": _p("decimal", "Quantity"),
        "weight": _p("decimal", "Weight (kg)"),
        "width": _p("decimal", "Width (cm)"),
        "length": _p("decimal", "Length (cm)"),
        "height": _p("decimal", "Height (cm)"),
    }
    return {
        "id": _p("string", "ID"),
        "quantity": _p("decimal", "Quantity"),
        "weight": _p("decimal", "Weight (kg)"),
        "width": _p("decimal", "Width (cm)"),
        "length": _p("decimal", "Length (cm)"),
        "height": _p("decimal", "Height (cm)"),
        "outerUnit": _p("embedded", "Outer unit", properties=outer),
    }


def _tax_accounts_props() -> dict[str, Any]:
    revenue = {
        key: _p("string", label)
        for key, label in (
            ("normal", "Normal"),
            ("reduced", "Reduced"),
            ("taxFree", "Tax-free"),
            ("intraCommunity", "Intra-community"),
            ("euNormal", "EU normal"),
            ("euReduced", "EU reduced"),
            ("nonTaxable", "Non-taxable"),
            ("export", "Export"),
        )
    }
    expense = {
        key: _p("string", label)
        for key, label in (
            ("normal", "Normal"),
            ("reduced", "Reduced"),
            ("taxFree", "Tax-free"),
            ("intraCommunity", "Intra-community"),
            ("euNormal", "EU normal"),
            ("euReduced", "EU reduced"),
            ("nonTaxable", "Non-taxable"),
            ("import", "Import"),
        )
    }
    return {
        "taxType": _p("integer", "Tax type"),
        "taxTypeDownload": _p("integer", "Tax type (download)"),
        "taxGroup": _p("integer", "Tax group"),
        "revenue": _p("embedded", "Revenue accounts", properties=revenue),
        "expense": _p("embedded", "Expense accounts", properties=expense),
        "texts": _p(
            "embedded",
            "Tax texts",
            properties={
                "intraCommunity": _p("string", "Intra-community"),
                "export": _p("string", "Export"),
            },
        ),
    }


# Include name → (label, section, node-properties builder).
_INCLUDE_COLLECTIONS: dict[str, tuple[str, str, Any]] = {
    "salesPrices": ("Sales prices", "pricing", _sales_prices_props),
    "purchasePrices": ("Purchase prices", "pricing", _purchase_prices_props),
    "commissions": ("Commissions", "pricing", _commissions_props),
    "deliveryThresholds": ("Delivery thresholds", "pricing", _delivery_thresholds_props),
    "calculationItems": ("Calculation items", "pricing", _calculation_items_props),
    "texts": ("Localized texts", "content", _texts_props),
    "media": ("Media", "content", _media_props),
    "properties": ("Properties", "content", _properties_props),
    "options": ("Options", "content", _options_props),
    "freeFields": ("Free fields", "content", _free_fields_props),
    "categories": ("Categories", "classification", _categories_props),
    "variants": ("Variants", "classification", _variants_props),
    "externalReferences": ("External references", "classification", _external_references_props),
    "salesChannels": ("Sales channels", "classification", _sales_channels_props),
    "tags": ("Tags", "classification", _tags_props),
    "parts": ("Parts (BOM)", "production", _bom_item_props),
    "usedIn": ("Used in", "production", _bom_item_props),
    "rawMaterials": ("Raw materials", "production", _raw_materials_props),
    "workInstructions": ("Work instructions", "production", _work_instructions_props),
    "functionProtocols": ("Function protocols", "production", _function_protocols_props),
    "crossSelling": ("Cross-selling", "production", _cross_selling_props),
    "certificates": ("Certificates", "production", _certificates_props),
    "stock": ("Stock (total)", "logistics", _stock_props),
    "storageLocations": ("Storage locations", "logistics", _storage_locations_props),
    "reservations": ("Reservations", "logistics", _reservations_props),
    "batches": ("Batches", "logistics", _batches_props),
    "serialNumbers": ("Serial numbers", "logistics", _serial_numbers_props),
    "bestBeforeDates": ("Best-before dates", "logistics", _best_before_dates_props),
    "warehouseMinimums": ("Warehouse minimums", "logistics", _warehouse_minimums_props),
    "packagingUnits": ("Packaging units", "logistics", _packaging_units_props),
}


class ProductAdapter:
    manifest = EmulationManifest(
        key="Product",
        label_en="Product",
        category="MasterData",
        rollout_batch="product-v3-readonly",
        adapter="basic-product-v3",
        source_apis=("/api/v3/products",),
        operations=("list", "read"),
    )

    base_path = "/api/v3/products"
    # Lean on list (base fields + tag pills); complete on detail (all includes).
    list_include = "tags"
    detail_include = ",".join(_ALL_INCLUDES)
    # Business-friendly filter/sort key → v3 wire key. Only ``project`` differs
    # (v3 filters it as ``project.id``); the rest match the v3 keys verbatim.
    query_aliases = {"project": "project.id", "projectId": "project.id"}
    search_fields = ("number", "name", "ean")
    # Only the fields the v3 list endpoint actually filters/sorts — advertising
    # any other would produce a 400 on the self-check.
    filterable_fields = (
        "number",
        "name",
        "ean",
        "project",
        "isVariant",
        "isMatrixProduct",
        "updatedAt",
    )
    sortable_fields = ("number", "name", "updatedAt")
    previewable_fields = ("number", "name", "ean", "isDisabled", "updatedAt")

    _allowed_cache: frozenset[str] | None = None

    # ── metadata (render contract) ────────────────────────────────────────
    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        del accept_language
        properties = self._root_properties()
        self._mark_capabilities(properties)
        self._mark_read_only(properties)
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "searchFields": list(self.search_fields),
            "previewTemplateString": "{{number}} · {{name}}",
            "sections": {
                "general": {"label": "General"},
                "classification": {"label": "Classification"},
                "content": {"label": "Content"},
                "pricing": {"label": "Pricing"},
                "production": {"label": "Production"},
                "logistics": {"label": "Logistics & stock"},
                "references": {"label": "References"},
                "commercial": {"label": "Commercial"},
                "tax": {"label": "Tax"},
                "origin": {"label": "Origin & customs"},
            },
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    def _mark_capabilities(self, properties: dict[str, Any]) -> None:
        """Stamp filterable/sortable/searchable/previewable + filter operators on
        the exact fields the v3 list endpoint supports."""
        for field in self.filterable_fields:
            spec = properties.get(field)
            if isinstance(spec, dict):
                spec["filterable"] = True
                spec["operators"] = list(_FILTER_OPS_BY_FIELD.get(field, _STRING_FILTER_OPS))
        for field in self.sortable_fields:
            if isinstance(properties.get(field), dict):
                properties[field]["sortable"] = True
        for field in self.search_fields:
            if isinstance(properties.get(field), dict):
                properties[field]["searchable"] = True
        for order, field in enumerate(self.previewable_fields):
            if isinstance(properties.get(field), dict):
                properties[field]["previewable"] = True
                properties[field]["previewOrder"] = order

    @staticmethod
    def _mark_read_only(properties: dict[str, Any]) -> None:
        """The whole v3 product surface is read-only — stamp every field (and
        every nested embedded/collection field) with ``access:"readOnly"`` so the
        render contract is honest, whatever consumer reads it."""

        def walk(props: dict[str, Any]) -> None:
            for spec in props.values():
                if not isinstance(spec, dict):
                    continue
                spec["access"] = "readOnly"
                nested = spec.get("properties")
                if isinstance(nested, dict):
                    walk(nested)
                node = spec.get("node")
                if isinstance(node, dict) and isinstance(node.get("properties"), dict):
                    walk(node["properties"])

        walk(properties)

    def _root_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            # general
            "id": _p("string", "ID"),
            "uuid": _p("string", "UUID"),
            "number": _p("string", "Product number", section="general", rules=["required"]),
            "name": _p("string", "Name", section="general", rules=["required"]),
            "description": _p("string", "Description", section="content"),
            "ean": _p("string", "EAN", section="general"),
            "unit": _p("string", "Unit", section="general"),
            "isStockItem": _p("boolean", "Stock item", section="general"),
            "isDisabled": _p("boolean", "Disabled", section="general"),
            "disabledReason": _p("string", "Disabled reason", section="general"),
            "isDeleted": _p("boolean", "Deleted", section="general"),
            "ageRating": _p("string", "Age rating", section="general"),
            "abcCategory": _p("string", "ABC category", section="general"),
            "noticeText": _p("string", "Notice text", section="content"),
            "createdAt": _p("datetime", "Created at", section="general"),
            "updatedAt": _p("datetime", "Updated at", section="general"),
            # classification
            "isVariant": _p("boolean", "Variant", section="classification"),
            "variantOf": _ref("Variant of", "Product", section="classification"),
            "isMatrixProduct": _p("boolean", "Matrix product", section="classification"),
            "merchandiseGroup": _ref(
                "Merchandise group", "ProductCategory", section="classification"
            ),
            # content
            "internalComment": _p("string", "Internal comment", section="content"),
            "documentQuantityFormula": _p("string", "Document quantity formula", section="content"),
            "documentPriceFormula": _p("string", "Document price formula", section="content"),
            # references
            "project": _ref("Project", "Project", section="references"),
            "standardSupplier": _ref("Standard supplier", "Supplier", section="references"),
            "manufacturerNumber": _p("string", "Manufacturer number", section="references"),
            "manufacturer": _p(
                "embedded",
                "Manufacturer",
                section="references",
                properties={
                    "name": _p("string", "Name"),
                    "number": _p("string", "Number"),
                    "link": _p("string", "Link"),
                },
            ),
            # origin & customs
            "customsTariffNumber": _p("string", "Customs tariff number", section="origin"),
            "countryOfOrigin": _p("string", "Country of origin", section="origin"),
            "regionOfOrigin": _p("string", "Region of origin", section="origin"),
            # tax
            "salesTax": _p(
                "select",
                "Sales tax",
                section="tax",
                options=[
                    {"value": "standard", "label": "Standard"},
                    {"value": "reduced", "label": "Reduced"},
                    {"value": "exempt", "label": "Exempt"},
                ],
            ),
            "individualTaxRate": _p("decimal", "Individual tax rate", section="tax"),
            "taxAccounts": _p(
                "embedded", "Tax accounts", section="tax", properties=_tax_accounts_props()
            ),
            # pricing
            "discount": _p(
                "embedded",
                "Discount",
                section="pricing",
                properties={
                    "isDiscountProduct": _p("boolean", "Discount product"),
                    "discountPercentage": _p("decimal", "Discount percentage"),
                },
            ),
            "calculatedPurchasePrice": _p(
                "embedded",
                "Calculated purchase price",
                section="pricing",
                properties={
                    "hasCalculatedPurchasePrice": _p("boolean", "Has calculated purchase price"),
                    "price": _money("Price"),
                },
            ),
            "calculatoryPurchasePriceSettings": _p(
                "embedded",
                "Calculatory purchase price settings",
                section="pricing",
                properties={
                    "addPurchasePriceToCalculationTriggers": _p(
                        "collection",
                        "Add-to-calculation triggers",
                        node={"properties": {"value": _p("string", "Trigger")}},
                    ),
                    "calculatoryPurchasePriceRecalculationTriggers": _p(
                        "collection",
                        "Recalculation triggers",
                        node={"properties": {"value": _p("string", "Trigger")}},
                    ),
                    "calculatoryPurchasePriceRecalculationMethod": _p(
                        "string", "Recalculation method"
                    ),
                },
            ),
            "noDiscountAllowed": _p("boolean", "No discount allowed", section="pricing"),
            "noCashDiscountAllowed": _p("boolean", "No cash discount allowed", section="pricing"),
            "isCommissionBlocked": _p("boolean", "Commission blocked", section="pricing"),
            "suppressSalesPriceWarning": _p(
                "boolean", "Suppress sales-price warning", section="pricing"
            ),
            "allowPurchaseFromAllSuppliers": _p(
                "boolean", "Allow purchase from all suppliers", section="pricing"
            ),
            "hasDailyPrices": _p("boolean", "Has daily prices", section="pricing"),
            "xentralVolumePoints": _p("decimal", "Xentral volume points", section="pricing"),
            # production
            "hasBillOfMaterials": _p("boolean", "Has bill of materials", section="production"),
            "isAssembledJustInTime": _p("boolean", "Assembled just in time", section="production"),
            "hideJustInTimeItemsOnDocuments": _p(
                "boolean", "Hide just-in-time items on documents", section="production"
            ),
            "isProductionProduct": _p("boolean", "Production product", section="production"),
            "isExternallyProduced": _p("boolean", "Externally produced", section="production"),
            "isCustomProduct": _p("boolean", "Custom product", section="production"),
            "hasRawMaterialList": _p("boolean", "Has raw-material list", section="production"),
            # logistics & stock
            "measurements": _p(
                "embedded",
                "Measurements",
                section="logistics",
                properties={
                    key: _p(
                        "embedded",
                        label,
                        properties={
                            "value": _p("decimal", "Value"),
                            "unit": _p("string", "Unit"),
                        },
                    )
                    for key, label in (
                        ("weight", "Weight"),
                        ("netWeight", "Net weight"),
                        ("length", "Length"),
                        ("width", "Width"),
                        ("height", "Height"),
                    )
                },
            ),
            "minimumStorageQuantity": _p(
                "integer", "Minimum storage quantity", section="logistics"
            ),
            "minimumOrderQuantity": _p("integer", "Minimum order quantity", section="logistics"),
            "serialNumbersMode": _p("string", "Serial-numbers mode", section="logistics"),
            "hasBatches": _p("boolean", "Has batches", section="logistics"),
            "hasBestBeforeDate": _p("boolean", "Has best-before date", section="logistics"),
            "isStockTakingDisabled": _p("boolean", "Stock-taking disabled", section="logistics"),
            "stockTaking": _p(
                "embedded",
                "Stock taking",
                section="logistics",
                properties={
                    "hasStockTakingValue": _p("boolean", "Has stock-taking value"),
                    "stockTakingValue": _p("decimal", "Stock-taking value"),
                },
            ),
            "defaultStorageLocation": _p("string", "Default storage location", section="logistics"),
            # commercial
            "isShippingCostsProduct": _p("boolean", "Shipping-costs product", section="commercial"),
            "hidePriceOnDocuments": _p("boolean", "Hide price on documents", section="commercial"),
            "requiresIdentityCheck": _p("boolean", "Requires identity check", section="commercial"),
            "isDevice": _p("boolean", "Device", section="commercial"),
            "isServiceProduct": _p("boolean", "Service product", section="commercial"),
            "isFee": _p("boolean", "Fee", section="commercial"),
            "isService": _p("boolean", "Service", section="commercial"),
            "requiresCustomerApproval": _p(
                "boolean", "Requires customer approval", section="commercial"
            ),
            "customerApprovalRule": _p("string", "Customer approval rule", section="commercial"),
        }
        # The 30 read-only sub-resource collections (includes).
        for name, (label, section, builder) in _INCLUDE_COLLECTIONS.items():
            props[name] = _coll(label, section, builder())
        return props

    # ── query translation (incoming contract → v3 wire) ───────────────────
    def _query(self, query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        # First pass: resolve each filter index to its wire key, so a value can
        # be reformatted based on the field it belongs to (datetime expansion).
        filter_key_by_index: dict[str, str] = {}
        for key, value in query:
            if key.startswith("filter[") and key.endswith("][key]"):
                index = key[len("filter[") : key.index("]")]
                filter_key_by_index[index] = self.query_aliases.get(value, value)

        translated: list[tuple[str, str]] = []
        has_per_page = False
        for key, value in query:
            if key == "page[size]":
                translated.append(("perPage", value))
                has_per_page = True
                continue
            if key == "page[number]":
                translated.append(("page", value))
                continue
            if key == "perPage":
                has_per_page = True
                translated.append((key, value))
                continue
            if key.endswith("[key]"):
                translated.append((key, self.query_aliases.get(value, value)))
                continue
            if key.startswith("filter[") and key.endswith("][value]"):
                index = key[len("filter[") : key.index("]")]
                resolved = filter_key_by_index.get(index)
                if resolved in _DATETIME_FILTER_KEYS and _BARE_DATE_RE.match(value or ""):
                    value = f"{value}T00:00:00+00:00"
                translated.append((key, value))
                continue
            if key == "sort":
                prefix = "-" if value.startswith("-") else ""
                field = value[1:] if prefix else value
                translated.append((key, prefix + self.query_aliases.get(field, field)))
                continue
            translated.append((key, value))
        translated = self._ensure_filter_ops(translated)
        if not has_per_page:
            translated.append(("perPage", "50"))
        return translated

    @staticmethod
    def _ensure_filter_ops(query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """v3 requires an ``op`` on every filter entry; default a keyed filter
        that arrives without one to ``equals``."""
        subfields: dict[str, set[str]] = {}
        for key, _ in query:
            if key.startswith("filter[") and "][" in key and key.endswith("]"):
                index = key[len("filter[") : key.index("]")]
                sub = key[key.index("][") + 2 : -1]
                subfields.setdefault(index, set()).add(sub)
        out = list(query)
        for index, subs in subfields.items():
            if "key" in subs and "op" not in subs:
                out.append((f"filter[{index}][op]", "equals"))
        return out

    @staticmethod
    def _request_headers(token: str, accept_language: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "xentral-ai-agent",
            # "table" mode so list meta carries total / lastPage (the workspace
            # table needs them); v3 selects the pager off this header.
            "X-Pagination": "table",
        }
        if accept_language:
            headers["Accept-Language"] = accept_language
        return headers

    @classmethod
    def _allowed_keys(cls) -> frozenset[str]:
        if cls._allowed_cache is None:
            cls._allowed_cache = frozenset(cls().metadata("en")["rootNode"]["properties"])
        return cls._allowed_cache

    @classmethod
    def _entity_record(cls, raw: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(raw)
        entity_id = record.get("id")
        record["id"] = str(entity_id) if entity_id is not None else None
        # v3 SHOW routes on a NUMERIC id (whereNumber). The self-check and the
        # generic detail view read a row back by its ``uuid`` when present, so
        # pin uuid to the numeric id — otherwise a read-by-uuid 400s. (Same
        # convention as the business-document adapters.)
        record["uuid"] = record["id"]
        allowed = cls._allowed_keys()
        return {key: value for key, value in record.items() if key in allowed}

    @staticmethod
    def _forward_headers(response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key in ("content-type", "etag", "cache-control", "x-pagination", "location")
            if (value := response.headers.get(key))
        }

    @classmethod
    def _json_response(
        cls,
        status_code: int,
        data: dict[str, Any],
        source_headers: httpx.Headers | None = None,
    ) -> AdapterResponse:
        headers = {"content-type": "application/json"}
        if source_headers is not None:
            headers.update(
                {
                    key: value
                    for key in ("etag", "cache-control", "x-pagination")
                    if (value := source_headers.get(key))
                }
            )
        return AdapterResponse(
            status_code=status_code,
            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )

    async def request(
        self,
        *,
        method: str,
        handle: str | None,
        query: list[tuple[str, str]],
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        method = method.upper()
        if method != "GET":
            # v3 products is read-only; the entity advertises only list/read.
            return self._json_response(
                405,
                {
                    "title": "Product is read-only",
                    "detail": (
                        "The v3 products endpoint only supports reading. Use the "
                        "SalesPrice, PurchasePrice, StorageLocation or PartsListItem "
                        "entities for writable sub-resources."
                    ),
                },
            )

        # The v3 products endpoint has no cross-field `search` key (only per-field
        # filters). Emulate a unified search as an OR fan-out over search_fields
        # (number/name/ean), the same way the other master-data adapters do.
        if not handle:
            search = extract_search(query)
            if search is not None:
                value, op = search
                return await fan_out_search(
                    self,
                    query=query,
                    value=value,
                    op=op,
                    search_fields=self.search_fields,
                    base_url=base_url,
                    token=token,
                    accept_language=accept_language,
                    client=client,
                )

        path = self.base_path
        if handle:
            if not handle.isdigit():
                return self._json_response(
                    400,
                    {"title": "Invalid Product handle", "detail": "Expected the numeric v3 id."},
                )
            path = f"{path}/{handle}"

        params = self._query(query)
        if not any(key == "include" for key, _ in params):
            params.append(("include", self.detail_include if handle else self.list_include))

        headers = self._request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            return await request_client.get(
                f"{base_url.rstrip('/')}{path}", params=params, headers=headers
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400 or not response.content:
            return AdapterResponse(
                response.status_code, response.content, self._forward_headers(response)
            )

        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code, response.content, self._forward_headers(response)
            )

        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data["data"] = [
                self._entity_record(row) if isinstance(row, dict) else row for row in data["data"]
            ]
        elif isinstance(data, dict) and isinstance(data.get("data"), dict):
            data["data"] = self._entity_record(data["data"])
        elif isinstance(data, dict):
            data = self._entity_record(data)

        return self._json_response(response.status_code, data, response.headers)
