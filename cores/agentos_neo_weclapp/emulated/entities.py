"""weclapp entity roster (curated, static) — read-only v1.

Covers the important ERP core entities across the sales + purchase cycle plus
master data, stock and configuration lookups, all read-only:

    Customer · Supplier · Article · Quotation · SalesOrder · SalesInvoice ·
    PurchaseOrder · PurchaseInvoice · GoodsReceipt · Shipment · Unit ·
    Currency · PaymentMethod · ProductCategory · Warehouse · StorageLocation ·
    Batch · SerialNumber · StockLevel · StockMovement · StockTake ·
    MerchandiseGroup · Tag · ShippingMethod · PaymentTermsGroup · TaxRate ·
    TextTemplate · Webhook · User

They share the same engine (``WeclappAdapterBase``) and are built from reusable
field bundles + two small factories (``_party_entity`` for the polymorphic
``/party`` roles, ``_business_document`` for line-item documents) so the shape is
consistent and the varying bits stay explicit.

weclapp property names below are reconciled against the real weclapp OpenAPI spec
(``https://www.weclapp.com/api/swagger.json``): the document number is
``orderNumber`` (not ``salesOrderNumber``), line-item lists are
``salesInvoiceItems`` / ``purchaseOrderItems``, currency is ``recordCurrencyId``,
a shipment links its party via ``recipientPartyId`` and tracks via
``packageTrackingNumber``. weclapp has no ``creditNote`` entity, so there is none
here. Statuses stay plain strings (not ``select``) — the per-document status
enums differ and a wrong option list is worse than none. The native
``weclapp_core`` mirrors the whole spec verbatim; this core is the curated view.
"""

from __future__ import annotations

from .base import Collection, Embed, Entity, Field, Reference, WeclappAdapterBase

# --- reusable field bundles --------------------------------------------------

# A postal address block (party address list entries + document address embeds).
_ADDRESS_FIELDS: tuple[Field, ...] = (
    Field("company", "Company", section="addresses"),
    Field("firstName", "First name", section="addresses"),
    Field("lastName", "Last name", section="addresses"),
    Field("street1", "Street", section="addresses"),
    Field("street2", "Street 2", section="addresses"),
    Field("zipcode", "ZIP", section="addresses"),
    Field("city", "City", section="addresses"),
    Field("state", "State", section="addresses"),
    Field("countryCode", "Country", section="addresses"),
)

# One line item of a business document. ``articleId`` is a reference to Article;
# the rest are the position's own scalars.
_LINE_ITEM_FIELDS: tuple[object, ...] = (
    Field("positionNumber", "Pos.", type="integer", section="items"),
    Reference("articleId", "Article", reference="Article", section="items"),
    Field("title", "Title", section="items"),
    Field("description", "Description", section="items"),
    Field("quantity", "Qty", type="decimal", section="items"),
    Field("unitPrice", "Unit price", type="decimal", section="items"),
    Field("netAmount", "Net", type="decimal", section="items"),
)


# --- party roles (Customer / Supplier over the polymorphic /party) -----------


def _party_entity(*, key: str, label: str, category: str, role: str) -> Entity:
    """A logical entity over the polymorphic ``/party`` endpoint, sliced to one
    role flag (``customer`` / ``supplier``). ``partyType`` distinguishes an
    organisation from a person."""
    return Entity(
        key=key,
        label_en=label,
        category=category,
        endpoint="party",
        label_field="company",
        sections=(
            ("general", "General"),
            ("contact", "Contact"),
            ("addresses", "Addresses"),
            ("classification", "Classification"),
            ("system", "System"),
        ),
        scalars=(
            Field(
                f"{role}Number",
                f"{label} no.",
                section="general",
                filterable=True,
                sortable=True,
                preview=0,
            ),
            Field(
                "company", "Company", section="general", filterable=True, sortable=True, preview=1
            ),
            Field("firstName", "First name", section="general", filterable=True),
            Field("lastName", "Last name", section="general", filterable=True),
            Field(
                "partyType",
                "Type",
                type="select",
                section="general",
                filterable=True,
                options=(("ORGANIZATION", "Organisation"), ("PERSON", "Person")),
            ),
            Field("email", "Email", section="contact", filterable=True, preview=2),
            Field("phone", "Phone", section="contact"),
            Field("website", "Website", section="contact"),
            Field("vatIdentificationNumber", "VAT ID", section="classification", filterable=True),
            Field(
                "customer", "Is customer", type="boolean", section="classification", filterable=True
            ),
            Field(
                "supplier", "Is supplier", type="boolean", section="classification", filterable=True
            ),
            Field(
                "createdDate",
                "Created",
                type="datetime",
                section="system",
                sortable=True,
                epoch=True,
            ),
            Field(
                "lastModifiedDate",
                "Updated",
                type="datetime",
                section="system",
                sortable=True,
                epoch=True,
            ),
        ),
        references=(Reference("currencyId", "Currency", reference="Currency", section="general"),),
        collections=(
            Collection("addresses", "Addresses", fields=_ADDRESS_FIELDS, section="addresses"),
        ),
        operations=("list", "read"),
        base_params=((f"{role}-eq", "true"),),
        additional_properties=("addresses",),
    )


CUSTOMER = _party_entity(key="Customer", label="Customer", category="crm", role="customer")
SUPPLIER = _party_entity(key="Supplier", label="Supplier", category="purchasing", role="supplier")


# --- Article (master data) ----------------------------------------------------

ARTICLE = Entity(
    key="Article",
    label_en="Article",
    category="products",
    endpoint="article",
    label_field="name",
    sections=(
        ("general", "General"),
        ("prices", "Prices"),
        ("descriptions", "Descriptions"),
        ("system", "System"),
    ),
    scalars=(
        Field(
            "articleNumber",
            "Article no.",
            section="general",
            filterable=True,
            sortable=True,
            preview=0,
        ),
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=1),
        Field("articleType", "Type", section="general", filterable=True, preview=2),
        Field("ean", "EAN", section="general", filterable=True),
        Field("active", "Active", type="boolean", section="general", filterable=True),
        Field("description", "Description", section="descriptions"),
        Field("internalNote", "Internal note", section="descriptions"),
        Field(
            "createdDate", "Created", type="datetime", section="system", sortable=True, epoch=True
        ),
        Field(
            "lastModifiedDate",
            "Updated",
            type="datetime",
            section="system",
            sortable=True,
            epoch=True,
        ),
    ),
    references=(Reference("unitId", "Unit", reference="Unit", section="general"),),
    collections=(
        Collection(
            "articlePrices",
            "Prices",
            fields=(
                Field("price", "Price", type="decimal", section="prices"),
                Field("startDate", "Valid from", type="date", section="prices", epoch=True),
                Reference("currencyId", "Currency", reference="Currency", section="prices"),
            ),
            section="prices",
        ),
    ),
    operations=("list", "read"),
    additional_properties=("articlePrices",),
)


# --- business documents (line-item documents) --------------------------------


def _business_document(
    *,
    key: str,
    label: str,
    category: str,
    endpoint: str,
    number_wire: str,
    date_wire: str,
    items_wire: str,
    items_label: str,
    party_wire: str,
    party_label: str,
    party_target: str,
    extra_scalars: tuple[Field, ...] = (),
    address_embeds: tuple[Embed, ...] = (),
    include_financials: bool = True,
) -> Entity:
    """A weclapp business document with a line-item collection. ``number_wire`` is
    the document number (also the preview/label field), ``date_wire`` the document
    date (epoch → date), ``items_wire`` the embedded line-item list, and
    ``party_wire`` the customer/supplier reference."""
    scalars: list[Field] = [
        Field(
            number_wire,
            f"{label} no.",
            key="documentNumber",
            section="general",
            filterable=True,
            sortable=True,
            preview=0,
        ),
        Field("status", "Status", section="general", filterable=True, sortable=True, preview=2),
        Field(
            date_wire,
            "Date",
            key="documentDate",
            type="date",
            section="general",
            sortable=True,
            epoch=True,
            preview=1,
        ),
    ]
    scalars.extend(extra_scalars)
    if include_financials:
        scalars.extend(
            [
                Field("netAmount", "Net", type="decimal", section="financials"),
                Field("grossAmount", "Gross", type="decimal", section="financials"),
            ]
        )
    scalars.extend(
        [
            Field(
                "createdDate",
                "Created",
                type="datetime",
                section="system",
                sortable=True,
                epoch=True,
            ),
            Field(
                "lastModifiedDate",
                "Updated",
                type="datetime",
                section="system",
                sortable=True,
                epoch=True,
            ),
        ]
    )
    references: list[Reference] = [
        Reference(
            party_wire,
            party_label,
            reference=party_target,
            key="party",
            section="references",
            preview=3,
        ),
    ]
    if include_financials:
        references.append(
            Reference(
                "recordCurrencyId",
                "Currency",
                reference="Currency",
                key="currencyId",
                section="financials",
            )
        )
    sections = [
        ("general", "General"),
        ("references", "References"),
        ("addresses", "Addresses"),
        ("items", "Line items"),
        ("financials", "Financials"),
        ("system", "System"),
    ]
    return Entity(
        key=key,
        label_en=label,
        category=category,
        endpoint=endpoint,
        label_field="documentNumber",
        sections=tuple(sections),
        scalars=tuple(scalars),
        references=tuple(references),
        embeds=address_embeds,
        collections=(
            Collection(items_wire, items_label, fields=_LINE_ITEM_FIELDS, section="items"),
        ),
        operations=("list", "read"),
        additional_properties=(items_wire, *(e.wire for e in address_embeds)),
    )


def _address_embed(wire: str, label: str) -> Embed:
    return Embed(wire, label, fields=_ADDRESS_FIELDS, section="addresses")


_DELIVERY = _address_embed("deliveryAddress", "Delivery address")
_INVOICE = _address_embed("invoiceAddress", "Invoice address")
_RECIPIENT = _address_embed("recipientAddress", "Recipient address")

QUOTATION = _business_document(
    key="Quotation",
    label="Quotation",
    category="sales",
    endpoint="quotation",
    number_wire="quotationNumber",
    date_wire="quotationDate",
    items_wire="quotationItems",
    items_label="Quotation items",
    party_wire="customerId",
    party_label="Customer",
    party_target="Customer",
    address_embeds=(_DELIVERY, _INVOICE),
)

SALES_ORDER = _business_document(
    key="SalesOrder",
    label="Sales order",
    category="sales",
    endpoint="salesOrder",
    number_wire="orderNumber",
    date_wire="orderDate",
    items_wire="orderItems",
    items_label="Order items",
    party_wire="customerId",
    party_label="Customer",
    party_target="Customer",
    extra_scalars=(
        Field("commission", "Commission", section="general", filterable=True),
        Field(
            "plannedDeliveryDate", "Planned delivery", type="date", section="general", epoch=True
        ),
    ),
    address_embeds=(_DELIVERY, _INVOICE),
)

SALES_INVOICE = _business_document(
    key="SalesInvoice",
    label="Sales invoice",
    category="accounting",
    endpoint="salesInvoice",
    number_wire="invoiceNumber",
    date_wire="invoiceDate",
    items_wire="salesInvoiceItems",
    items_label="Invoice items",
    party_wire="customerId",
    party_label="Customer",
    party_target="Customer",
    extra_scalars=(
        Field("dueDate", "Due", type="date", section="general", sortable=True, epoch=True),
        Field("paid", "Paid", type="boolean", section="financials", filterable=True),
        Field("paymentStatus", "Payment status", section="general", filterable=True),
    ),
    address_embeds=(_INVOICE,),
)

PURCHASE_ORDER = _business_document(
    key="PurchaseOrder",
    label="Purchase order",
    category="purchasing",
    endpoint="purchaseOrder",
    number_wire="purchaseOrderNumber",
    date_wire="orderDate",
    items_wire="purchaseOrderItems",
    items_label="Order items",
    party_wire="supplierId",
    party_label="Supplier",
    party_target="Supplier",
    address_embeds=(_DELIVERY,),
)

# Shipment: a document without financial totals. weclapp links it to a party via
# ``recipientPartyId`` (not ``customerId``) and tracks via ``packageTrackingNumber``.
SHIPMENT = _business_document(
    key="Shipment",
    label="Shipment",
    category="logistics",
    endpoint="shipment",
    number_wire="shipmentNumber",
    date_wire="shippingDate",
    items_wire="shipmentItems",
    items_label="Shipment items",
    party_wire="recipientPartyId",
    party_label="Recipient",
    party_target="Customer",
    extra_scalars=(Field("packageTrackingNumber", "Tracking", section="general", filterable=True),),
    address_embeds=(_RECIPIENT,),
    include_financials=False,
)


# --- master-data lookups + stock (read-only) ---------------------------------
#
# weclapp property names reconciled against the OpenAPI spec vendored in the
# native ``weclapp_core`` (cores/weclapp_core/openapi.json). Keys lean on the
# Neo model's vocabulary (StockLevel/StorageLocation/Batch/SerialNumber/Unit/…)
# so this facade converges with ``agentos_neo_xentral`` — and, incidentally,
# resolves the ``Unit``/``Currency`` references Article and the documents
# already emit.

_SYSTEM_STAMPS: tuple[Field, ...] = (
    Field("createdDate", "Created", type="datetime", section="system", sortable=True, epoch=True),
    Field(
        "lastModifiedDate", "Updated", type="datetime", section="system", sortable=True, epoch=True
    ),
)
_LOOKUP_SECTIONS = (("general", "General"), ("system", "System"))

UNIT = Entity(
    key="Unit",
    label_en="Unit",
    category="products",
    endpoint="unit",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("description", "Description", section="general"),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

CURRENCY = Entity(
    key="Currency",
    label_en="Currency",
    category="accounting",
    endpoint="currency",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("currencySymbol", "Symbol", section="general", preview=1),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

PAYMENT_METHOD = Entity(
    key="PaymentMethod",
    label_en="Payment method",
    category="accounting",
    endpoint="paymentMethod",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("type", "Type", section="general", filterable=True, preview=1),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

PRODUCT_CATEGORY = Entity(
    key="ProductCategory",
    label_en="Product category",
    category="products",
    endpoint="articleCategory",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("description", "Description", section="general"),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("parentCategoryId", "Parent", reference="ProductCategory", section="general"),
    ),
    operations=("list", "read"),
)

WAREHOUSE = Entity(
    key="Warehouse",
    label_en="Warehouse",
    category="logistics",
    endpoint="warehouse",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("shortIdentifier", "Short id", section="general", filterable=True, preview=1),
        Field("warehouseType", "Type", section="general", filterable=True),
        Field("active", "Active", type="boolean", section="general", filterable=True),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

STORAGE_LOCATION = Entity(
    key="StorageLocation",
    label_en="Storage location",
    category="logistics",
    endpoint="storagePlace",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("barcode", "Barcode", section="general", filterable=True, preview=1),
        Field("storagePlaceType", "Type", section="general", filterable=True),
        Field("active", "Active", type="boolean", section="general", filterable=True),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("warehouseId", "Warehouse", reference="Warehouse", section="general", preview=2),
    ),
    operations=("list", "read"),
)

BATCH = Entity(
    key="Batch",
    label_en="Batch",
    category="logistics",
    endpoint="batchNumber",
    label_field="batchNumber",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field(
            "batchNumber", "Number", section="general", filterable=True, sortable=True, preview=0
        ),
        Field("expirationDate", "Best before", type="date", section="general", epoch=True),
        Field("ddsNumber", "DDS number", section="general"),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("articleId", "Article", reference="Article", section="general", preview=1),
    ),
    operations=("list", "read"),
)

SERIAL_NUMBER = Entity(
    key="SerialNumber",
    label_en="Serial number",
    category="logistics",
    endpoint="serialNumber",
    label_field="serialNumber",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field(
            "serialNumber", "Number", section="general", filterable=True, sortable=True, preview=0
        ),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("articleId", "Article", reference="Article", section="general", preview=1),
    ),
    operations=("list", "read"),
)

# The stock query: weclapp ``warehouseStock`` is the product × warehouse ×
# storage place × batch projection with an on-hand quantity.
STOCK_LEVEL = Entity(
    key="StockLevel",
    label_en="Stock level",
    category="logistics",
    endpoint="warehouseStock",
    label_field="quantity",
    sections=(("general", "General"), ("system", "System")),
    scalars=(
        Field("quantity", "Quantity", type="decimal", section="general", sortable=True, preview=0),
        Field("inboundDate", "Inbound", type="date", section="general", epoch=True),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("articleId", "Article", reference="Article", section="general", preview=1),
        Reference("warehouseId", "Warehouse", reference="Warehouse", section="general", preview=2),
        Reference(
            "storagePlaceId", "Storage location", reference="StorageLocation", section="general"
        ),
        Reference("batchNumberId", "Batch", reference="Batch", section="general"),
    ),
    operations=("list", "read"),
)


# --- more business documents --------------------------------------------------

PURCHASE_INVOICE = _business_document(
    key="PurchaseInvoice",
    label="Purchase invoice",
    category="accounting",
    endpoint="purchaseInvoice",
    number_wire="invoiceNumber",
    date_wire="invoiceDate",
    items_wire="purchaseInvoiceItems",
    items_label="Invoice items",
    party_wire="supplierId",
    party_label="Supplier",
    party_target="Supplier",
    extra_scalars=(
        Field("dueDate", "Due", type="date", section="general", sortable=True, epoch=True),
        Field("paymentStatus", "Payment status", section="general", filterable=True),
    ),
    address_embeds=(_INVOICE,),
)

# Goods receipt (weclapp ``incomingGoods``) built directly: it has no document
# date field (only createdDate) and carries a warehouse reference the document
# factory does not model, so it is declared explicitly.
GOODS_RECEIPT = Entity(
    key="GoodsReceipt",
    label_en="Goods receipt",
    category="logistics",
    endpoint="incomingGoods",
    label_field="documentNumber",
    sections=(
        ("general", "General"),
        ("references", "References"),
        ("items", "Line items"),
        ("system", "System"),
    ),
    scalars=(
        Field(
            "incomingGoodsNumber",
            "Goods receipt no.",
            key="documentNumber",
            section="general",
            filterable=True,
            sortable=True,
            preview=0,
        ),
        Field("status", "Status", section="general", filterable=True, sortable=True, preview=2),
        Field("deliveryNoteNumber", "Delivery note no.", section="general", filterable=True),
        Field(
            "createdDate",
            "Created",
            key="documentDate",
            type="datetime",
            section="general",
            sortable=True,
            epoch=True,
            preview=1,
        ),
        Field(
            "lastModifiedDate",
            "Updated",
            type="datetime",
            section="system",
            sortable=True,
            epoch=True,
        ),
    ),
    references=(
        Reference(
            "senderPartyId",
            "Supplier",
            reference="Supplier",
            key="party",
            section="references",
            preview=3,
        ),
        Reference("warehouseId", "Warehouse", reference="Warehouse", section="references"),
    ),
    collections=(
        Collection(
            "incomingGoodsItems", "Receipt items", fields=_LINE_ITEM_FIELDS, section="items"
        ),
    ),
    operations=("list", "read"),
    additional_properties=("incomingGoodsItems",),
)


# --- inventory document + more configuration lookups -------------------------
#
# weclapp entities without a faithful Neo counterpart are deliberately NOT
# mapped: ``pick`` is task-level (weclapp has no picking-*run* grouping),
# ``shipmentReturnReason`` has no component schema to reconcile, and weclapp has
# no ``deliveryNote`` / ``creditNote`` / ``priceList`` / standalone ``payment``
# entity at all (see the native ``weclapp_core`` mirror for the raw surface).

STOCK_TAKE = Entity(
    key="StockTake",
    label_en="Stock take",
    category="logistics",
    endpoint="inventory",
    label_field="number",
    sections=(("general", "General"), ("system", "System")),
    scalars=(
        Field(
            "inventoryNumber",
            "Number",
            key="number",
            section="general",
            filterable=True,
            sortable=True,
            preview=0,
        ),
        Field("status", "Status", section="general", filterable=True, sortable=True, preview=2),
        Field("description", "Description", section="general", preview=1),
        Field("startDate", "Started", type="date", section="general", sortable=True, epoch=True),
        Field("endDate", "Ended", type="date", section="general", sortable=True, epoch=True),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("warehouseId", "Warehouse", reference="Warehouse", section="general", preview=3),
    ),
    operations=("list", "read"),
)

TAG = Entity(
    key="Tag",
    label_en="Tag",
    category="crm",
    endpoint="tag",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

SHIPPING_METHOD = Entity(
    key="ShippingMethod",
    label_en="Shipping method",
    category="logistics",
    endpoint="shipmentMethod",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("description", "Description", section="general", preview=1),
        Field("active", "Active", type="boolean", section="general", filterable=True),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

PAYMENT_TERMS_GROUP = Entity(
    key="PaymentTermsGroup",
    label_en="Payment terms",
    category="accounting",
    endpoint="termOfPayment",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("description", "Description", section="general", preview=1),
        Field("numberOfDays", "Days", type="integer", section="general"),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

TAX_RATE = Entity(
    key="TaxRate",
    label_en="Tax rate",
    category="accounting",
    endpoint="tax",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("taxValue", "Rate %", type="decimal", section="general", filterable=True, preview=1),
        Field("taxKey", "Tax key", section="general", filterable=True),
        Field("taxType", "Type", section="general", filterable=True),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

WEBHOOK = Entity(
    key="Webhook",
    label_en="Webhook",
    category="integrations",
    endpoint="webhook",
    label_field="url",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("url", "URL", section="general", filterable=True, preview=0),
        Field("entityName", "Entity", section="general", filterable=True, preview=1),
        Field("requestMethod", "Method", section="general", filterable=True),
        Field("atCreate", "On create", type="boolean", section="general"),
        Field("atUpdate", "On update", type="boolean", section="general"),
        Field("atDelete", "On delete", type="boolean", section="general"),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

USER = Entity(
    key="User",
    label_en="User",
    category="organization",
    endpoint="user",
    label_field="username",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("username", "Username", section="general", filterable=True, sortable=True, preview=0),
        Field("email", "Email", section="general", filterable=True, preview=1),
        Field("firstName", "First name", section="general", filterable=True),
        Field("lastName", "Last name", section="general", filterable=True),
        Field("status", "Status", section="general", filterable=True),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)


# The stock LEDGER (append-only movements) — weclapp `warehouseStockMovement`,
# complementing StockLevel (the on-hand projection). weclapp records one
# storage place + a movement type per row; the Neo model's richer from/to split
# is not represented upstream, so this maps the flat weclapp shape faithfully
# (type stays a plain string — weclapp's enum differs from ours).
STOCK_MOVEMENT = Entity(
    key="StockMovement",
    label_en="Stock movement",
    category="logistics",
    endpoint="warehouseStockMovement",
    label_field="number",
    sections=(("general", "General"), ("system", "System")),
    scalars=(
        Field(
            "movementNumber",
            "Number",
            key="number",
            section="general",
            filterable=True,
            sortable=True,
            preview=0,
        ),
        Field(
            "stockMovementType", "Type", key="type", section="general", filterable=True, preview=2
        ),
        Field(
            "postingDate",
            "Date",
            key="date",
            type="date",
            section="general",
            sortable=True,
            epoch=True,
            preview=1,
        ),
        Field("quantity", "Quantity", type="decimal", section="general", sortable=True),
        Field(
            "valuationPrice", "Valuation price", key="unitCost", type="decimal", section="general"
        ),
        Field("movementNote", "Note", key="note", section="general"),
        *_SYSTEM_STAMPS,
    ),
    references=(
        Reference("articleId", "Article", reference="Article", section="general", preview=3),
        Reference(
            "storagePlaceId", "Storage location", reference="StorageLocation", section="general"
        ),
        Reference("batchNumberId", "Batch", reference="Batch", section="general"),
        Reference("userId", "User", reference="User", section="general"),
    ),
    operations=("list", "read"),
)

MERCHANDISE_GROUP = Entity(
    key="MerchandiseGroup",
    label_en="Merchandise group",
    category="products",
    endpoint="articleItemGroup",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)

TEXT_TEMPLATE = Entity(
    key="TextTemplate",
    label_en="Text template",
    category="crm",
    endpoint="mailTemplate",
    label_field="name",
    sections=_LOOKUP_SECTIONS,
    scalars=(
        Field("name", "Name", section="general", filterable=True, sortable=True, preview=0),
        Field("type", "Type", section="general", filterable=True, preview=1),
        Field("subject", "Subject", section="general"),
        Field("useAsDefault", "Default", type="boolean", section="general", filterable=True),
        *_SYSTEM_STAMPS,
    ),
    operations=("list", "read"),
)


_ENTITIES: tuple[Entity, ...] = (
    CUSTOMER,
    SUPPLIER,
    ARTICLE,
    QUOTATION,
    SALES_ORDER,
    SALES_INVOICE,
    PURCHASE_ORDER,
    PURCHASE_INVOICE,
    GOODS_RECEIPT,
    SHIPMENT,
    # master-data lookups + stock
    UNIT,
    CURRENCY,
    PAYMENT_METHOD,
    PRODUCT_CATEGORY,
    MERCHANDISE_GROUP,
    WAREHOUSE,
    STORAGE_LOCATION,
    BATCH,
    SERIAL_NUMBER,
    STOCK_LEVEL,
    STOCK_MOVEMENT,
    STOCK_TAKE,
    # configuration lookups
    TAG,
    SHIPPING_METHOD,
    PAYMENT_TERMS_GROUP,
    TAX_RATE,
    TEXT_TEMPLATE,
    WEBHOOK,
    USER,
)


def build_adapters() -> tuple[WeclappAdapterBase, ...]:
    """The weclapp entity roster. Static (weclapp has no live schema discovery),
    so it never touches the tenant and never raises — an unconfigured connection
    simply yields empty data on the request path, not a broken catalogue."""
    return tuple(WeclappAdapterBase(e) for e in _ENTITIES)
