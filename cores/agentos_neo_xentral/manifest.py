"""Xentral V3 — facade / translation core (Mode C). WIP scaffold.

Outward it speaks the redesigned, agent-friendly payload model (sprechende IDs,
reference objects, one document skeleton, generated filters, status + actions);
inward it maps to today's Xentral API (v3 + v1/v2 remnants) with NO own
persistence — a pure read-composer + write-orchestrator facade.

This is the SCAFFOLD only: no adapters yet. The per-entity facade adapters land
in ``emulated/`` once we start building. The full concept lives in ``docs/``
(00-decisions … 05-fehlende-apis, plus the two backlog CSVs); the authoring
pattern is the "Mode C" section of ``docs/guides/building-an-erp-core.md``.

Gaps are surfaced the same way as the other cores: not-yet-writable fields and
missing resources are marked blue (``priorities.json``) and what actually works
is proven by live tests (``verified.json``) — the core carries its own backlog.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly
from .emulated.channel import ChannelAdapter
from .emulated.correspondence import CorrespondenceAdapter
from .emulated.email_account import EmailAccountAdapter
from .emulated.credit_note import CreditNoteAdapter
from .emulated.customer import CustomerAdapter
from .emulated.delivery_note import DeliveryNoteAdapter
from .emulated.goods_receipt import GoodsReceiptAdapter
from .emulated.batch import BatchAdapter
from .emulated.payment import PaymentAdapter
from .emulated.picking_run import PickingRunAdapter
from .emulated.price_list import PriceListAdapter
from .emulated.printer import PrinterAdapter
from .emulated.product import ProductAdapter
from .emulated.purchase_invoice import PurchaseInvoiceAdapter
from .emulated.purchase_order import PurchaseOrderAdapter
from .emulated.quote import QuoteAdapter
from .emulated.return_order import ReturnAdapter
from .emulated.sales_invoice import SalesInvoiceAdapter
from .emulated.sales_order import SalesOrderAdapter
from .emulated.serial_number import SerialNumberAdapter
from .emulated.settings import SETTINGS_ADAPTERS
from .emulated.shipment import ShipmentAdapter
from .emulated.stock_level import StockLevelAdapter
from .emulated.stock_movement import StockMovementAdapter
from .emulated.stock_take import StockTakeAdapter
from .emulated.storage_location import StorageLocationAdapter
from .emulated.supplier import SupplierAdapter
from .emulated.tag import TagAdapter

CORE = CoreManifest(
    id="agentos_neo_xentral",
    label_de="AgentOS Neo (based on Xentral)",
    label_en="AgentOS Neo (based on Xentral)",
    order=8,
    featured=True,
    native_policy=EmulatedOnly(),
    adapters=(
        QuoteAdapter(),
        SalesOrderAdapter(),
        SalesInvoiceAdapter(),
        DeliveryNoteAdapter(),
        CreditNoteAdapter(),
        ReturnAdapter(),
        PurchaseOrderAdapter(),
        GoodsReceiptAdapter(),
        PurchaseInvoiceAdapter(),
        CustomerAdapter(),
        SupplierAdapter(),
        ChannelAdapter(),
        ProductAdapter(),
        PriceListAdapter(),
        ShipmentAdapter(),
        PaymentAdapter(),
        StockMovementAdapter(),
        StorageLocationAdapter(),
        StockLevelAdapter(),
        StockTakeAdapter(),
        PickingRunAdapter(),
        BatchAdapter(),
        SerialNumberAdapter(),
        TagAdapter(),
        # CRM tab on the customer record (replaces the xentral_crm MCP tool).
        CorrespondenceAdapter(),
        # Real-world device/communication surfaces (replace the xentral_email
        # MCP tool; printing ported from the xentral_api core).
        PrinterAdapter(),
        EmailAccountAdapter(),
        # Read-only settings/configuration lookups (category "settings") — the
        # instance's setup catalogue as entities, folding the standalone
        # xentral_erp_settings tool's resources into the core.
        *SETTINGS_ADAPTERS,
    ),  # facade adapters added per entity as we build
    description_de=(
        "Das agentenfreundliche Modell der nächsten Generation: ein neu "
        "entworfenes, vereinfachtes Set an Geschäftsobjekten, live auf deine "
        "bestehenden Xentral-Daten abgebildet."
    ),
    description_en=(
        "The agent-friendly next-generation model: a redesigned, simplified "
        "set of business objects, mapped live onto your existing Xentral data."
    ),
)
