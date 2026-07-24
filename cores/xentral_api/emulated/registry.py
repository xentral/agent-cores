"""Xentral Basic — the core's own emulated-adapter pool.

Every emulated business object this core exposes is defined and owned here, so
Xentral Basic is fully independent of any other core: nothing is pulled from a
shared adapter pool. The shared ``entity_registry.emulated`` package provides
only the engine (adapter base class, gateway, composition machinery); the
concrete entity definitions live in this package.

The tuple order is the display order in the entity registry. To add an entity,
drop its adapter module in this package and append the instance below.
"""

from __future__ import annotations

from entity_registry.core_sdk import EmulatedEntityAdapter

from .credit_note import CreditNoteAdapter
from .customer import CustomerAdapter
from .delivery_note import DeliveryNoteAdapter
from .invoice import InvoiceAdapter
from .parts_list_item import PartsListItemAdapter
from .printer import PrinterAdapter
from .product import ProductAdapter
from .purchase_order import PurchaseOrderAdapter
from .purchase_price import PurchasePriceAdapter
from .report import ReportAdapter
from .sales_order import SalesOrderAdapter
from .sales_price import SalesPriceAdapter
from .shipping_order import ShippingOrderAdapter
from .storage_location import StorageLocationAdapter
from .supplier import SupplierAdapter
from .supplier_invoice import SupplierInvoiceAdapter
from .v3_documents import (
    OfferAdapter,
    PriceInquiryAdapter,
    ProductionOrderAdapter,
    ProformaInvoiceAdapter,
    ReturnOrderAdapter,
)

_ADAPTERS: tuple[EmulatedEntityAdapter, ...] = (
    CustomerAdapter(),
    SupplierAdapter(),
    SalesOrderAdapter(),
    InvoiceAdapter(),
    CreditNoteAdapter(),
    ProductAdapter(),
    PartsListItemAdapter(),
    PurchasePriceAdapter(),
    SalesPriceAdapter(),
    StorageLocationAdapter(),
    PrinterAdapter(),
    ReportAdapter(),
    ShippingOrderAdapter(),
    PurchaseOrderAdapter(),
    DeliveryNoteAdapter(),
    ReturnOrderAdapter(),
    OfferAdapter(),
    ProformaInvoiceAdapter(),
    PriceInquiryAdapter(),
    ProductionOrderAdapter(),
    # Basic-local curated passthrough: CRUD forwards to the native Business
    # Entity API while this core owns the metadata and tag actions.
    SupplierInvoiceAdapter(),
)


def adapters() -> tuple[EmulatedEntityAdapter, ...]:
    """This core's full, ordered emulated-adapter set."""
    return _ADAPTERS
