"""Regression: vatId must round-trip through the v3 ``financials.tax.vatId`` path.

Customer and Supplier both expose a writable ``vatId``. On the v3 ``/customers``
and ``/suppliers`` resources the VAT id lives at ``financials.tax.vatId`` — NOT on
``primaryAddress`` and NOT at a top-level ``tax`` block. Writing it to the wrong
place made v3 silently drop the value (200/201 with vatId lost). These tests pin
the correct nesting on both write and read for both adapters.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo.emulated.supplier import SupplierAdapter


@pytest.mark.parametrize("adapter_cls", [CustomerAdapter, SupplierAdapter])
def test_vatid_writes_to_financials_tax(adapter_cls):
    v3, rejected = adapter_cls().map_write({"vatId": "DE112233445"}, creating=True)

    # Correct nesting, and NOT the two wrong places it used to land in.
    assert v3.get("financials", {}).get("tax", {}).get("vatId") == "DE112233445"
    assert "tax" not in v3  # not a top-level tax block
    assert "vatId" not in (v3.get("primaryAddress") or {})  # not on the address
    # vatId is a declared writable field — it must never be rejected.
    assert "vatId" not in rejected


@pytest.mark.parametrize("adapter_cls", [CustomerAdapter, SupplierAdapter])
def test_vatid_reads_from_financials_tax(adapter_cls):
    model = adapter_cls().map_read(
        {"financials": {"tax": {"vatId": "DE112233445"}}, "primaryAddress": {"name": "X"}}
    )
    assert model["vatId"] == "DE112233445"


@pytest.mark.parametrize("adapter_cls", [CustomerAdapter, SupplierAdapter])
def test_vatid_absent_is_none_not_error(adapter_cls):
    # No financials block on the wire → vatId reads as None, no crash.
    assert adapter_cls().map_read({"primaryAddress": {"name": "X"}})["vatId"] is None
    # Omitting vatId on write must not emit an empty financials/tax block.
    v3, _ = adapter_cls().map_write({"name": "X"}, creating=True)
    assert "financials" not in v3
