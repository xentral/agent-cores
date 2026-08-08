"""The order a business reads its own system in.

Two documents are written in this sequence — `erp-spec.yaml`, where the requirement is
written, and `review.xlsx`, where it is reviewed — so someone moving between them is at
the same place in both. Alphabetically, `CreditNote` would come first among the
documents and `Return` eleventh: the two most often looked at together, ten screens
apart.

It lives in the core rather than in either consumer for the same reason `verdicts.py`
does: a sequence copied into a test and into an exporter is two sequences, and the test
would then be guarding its own copy.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Customer → quote → order → delivery → invoice, then the return path, then
#: purchasing, then the master data those documents draw on. Anything not named here
#: sorts alphabetically after it — a new entity lands at the end of its category
#: rather than silently in the middle of the sales chain.
CHAIN: tuple[str, ...] = (
    "Customer",
    "Quote",
    "SalesOrder",
    "DeliveryNote",
    "Shipment",
    "SalesInvoice",
    "Payment",
    "Return",
    "CreditNote",
    "Supplier",
    "PurchaseOrder",
    "GoodsReceipt",
    "PurchaseInvoice",
    "Product",
    "PriceList",
    "PurchasePrice",
    "StorageLocation",
    "StockLevel",
    "StockMovement",
    "StockTake",
    "PickingRun",
    "Warehouse",
)

_RANK = {key: index for index, key in enumerate(CHAIN)}


def chain_order(keys: Iterable[str]) -> list[str]:
    """Process order first, then alphabetical."""
    return sorted(keys, key=lambda key: (_RANK.get(key, len(CHAIN)), key))
