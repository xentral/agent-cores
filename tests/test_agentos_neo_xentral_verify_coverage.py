"""verify.py coverage selectors — the field/facet selection is pure and testable
without a live tenant. Pins that the extended update roundtrip now reaches numeric/
boolean leaves and that the create probe covers Product/PriceList/PurchasePrice,
including the fields the null-restore update roundtrip cannot reach.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.checks import verify as V
from xentral_entity_cores.agentos_neo_xentral.emulated.price_list import PriceListAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_price import PurchasePriceAdapter


def test_update_targets_now_include_numeric_and_boolean_leaves():
    paths = {p for p, _ in V._update_targets(ProductAdapter().fields())}
    # numeric + boolean leaves that the old string/date-only selector missed
    assert "logistics.minimumOrderQuantity" in paths
    assert "logistics.dimensions.length" in paths
    assert "tracking.stock" in paths
    assert "documentDefaults.hidePrice" in paths
    assert "production.hasBillOfMaterials" in paths
    # still includes the string leaves
    assert {"name", "description", "manufacturer.website", "tracking.serialNumbers"} <= paths
    # collections are still NOT update-targets (need per-item probes)
    assert not any(p.startswith("suppliers") or p.startswith("bom") for p in paths)


def test_product_create_payload_sets_the_blindspot_fields():
    body, expects = V._simple_create_payload("Product", ProductAdapter().fields(), "61078", "13")
    # the two fields the net-zero update roundtrip cannot reach (null on samples)
    assert body["logistics"]["minimumStockQuantity"] == 25
    assert body["documentDefaults"]["hidePrice"] is True
    paths = {p for p, _, _ in expects}
    assert {
        "description",
        "manufacturer.website",
        "logistics.minimumStockQuantity",
        "documentDefaults.hidePrice",
        "prices.sale.amount",
    } <= paths


def test_price_list_create_payload_is_a_tier():
    body, expects = V._simple_create_payload(
        "PriceList", PriceListAdapter().fields(), "61078", None
    )
    assert body["product"] == {"id": "prd_61078"}
    assert body["minQuantity"] == 10
    assert body["unitPrice"]["amount"] == "7.50"
    assert ("minQuantity", "num", 10) in expects


def test_purchase_price_create_payload_carries_supplier_and_tier():
    fields = PurchasePriceAdapter().fields()
    body, expects = V._simple_create_payload("PurchasePrice", fields, "61078", "13")
    assert body["product"] == {"id": "prd_61078"}
    assert body["supplier"] == {"id": "sup_13"}
    assert body["minQuantity"] == 10
    assert ("supplier", "ref", "13") in expects
    # without a supplier fixture the probe still works (supplier optional on create)
    body2, expects2 = V._simple_create_payload("PurchasePrice", fields, "61078", None)
    assert "supplier" not in body2
    assert not any(p == "supplier" for p, _, _ in expects2)


def test_price_entities_have_full_write_ops():
    for A in (PriceListAdapter, PurchasePriceAdapter):
        ops = set(A().manifest.operations)
        assert {"create", "update", "delete"} <= ops
