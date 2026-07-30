"""Every draft-able document lifts from draft the same way: `release`.

"Release" (Freigeben) is the ERP term for taking a document out of draft — it
becomes valid and gets its number from the number range. v3 exposes it uniformly
as the `release` action on every document, so the neutral op is the same name
across all document cores (not per-document confirm/issue/etc.).
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

_DOC_ADAPTERS = [
    SalesOrderAdapter,
    ReturnAdapter,
    QuoteAdapter,
    SalesInvoiceAdapter,
    DeliveryNoteAdapter,
    CreditNoteAdapter,
    PurchaseOrderAdapter,
]


@pytest.mark.parametrize("adapter_cls", _DOC_ADAPTERS)
def test_release_is_wired_to_v3_release(adapter_cls):
    a = adapter_cls()
    assert a.action_map.get("release") == ("PATCH", "release")


@pytest.mark.parametrize("adapter_cls", _DOC_ADAPTERS)
def test_release_is_a_document_status_step(adapter_cls):
    groups = {g["key"]: g for g in adapter_cls().steps()}
    keys = {c["key"] for c in groups.get("documentStatus", {}).get("commands", [])}
    assert "release" in keys


@pytest.mark.parametrize("adapter_cls", _DOC_ADAPTERS)
def test_no_legacy_confirm_or_issue_op(adapter_cls):
    # the draft-lift is uniformly 'release' — the old per-document names are gone
    am = adapter_cls().action_map
    assert "confirm" not in am and "issue" not in am


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
