"""SalesOrder surfaces its fulfilment traffic lights.

The read passes ALL upstream traffic lights through untouched as `trafficLights`
(the same Ampeln the UI shows), and derives `holds` — the curated, evidence-based
subset that actually blocks a dispatch. Verified against mvp: an out-of-stock
order shows stock=false / stockAvailable*="no" while passing checks are true, and
dispatch is rejected with "Check items in stock not passed".
"""

from __future__ import annotations

from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


def _read(lights: list[dict[str, Any]]) -> dict[str, Any]:
    raw = {"id": 22912, "financials": {"currency": "EUR"}, "lineItems": [], "trafficLights": lights}
    return SalesOrderAdapter().map_read(raw)


# The exact set observed on mvp for an out-of-stock (blocked) order.
_BLOCKED = [
    {"id": "stock", "type": "system", "state": False},
    {"id": "stockAvailableOpenSupply", "type": "system", "state": "no"},
    {"id": "stockAvailableFifo", "type": "system", "state": "no"},
    {"id": "payment", "type": "system", "state": "fullyPaid"},
    {"id": "creditLimit", "type": "system", "state": True},
    {"id": "deliveryBlock", "type": "system", "state": True},
    {"id": "addressValidation", "type": "system", "state": True},
    {"id": "1", "type": "custom", "state": False},
]


def test_stock_block_becomes_a_hold():
    rec = _read(_BLOCKED)
    holds = rec["holds"]
    assert [h["type"] for h in holds] == ["stock"]  # one stock hold, de-duplicated
    assert holds[0]["state"] == "false"


def test_all_lights_pass_through_raw():
    rec = _read(_BLOCKED)
    lights = {light["id"]: light["state"] for light in rec["trafficLights"]}
    # every signal is visible, booleans normalized to strings
    assert lights["stock"] == "false"
    assert lights["payment"] == "fullyPaid"
    assert lights["creditLimit"] == "true"
    assert lights["stockAvailableFifo"] == "no"


def test_passing_checks_are_not_holds():
    # creditLimit/deliveryBlock/address = true (ok) → no hold; payment fullyPaid → no hold.
    rec = _read(_BLOCKED)
    assert not any(h["type"] in {"creditLimit", "manual", "address"} for h in rec["holds"])


def test_green_order_has_no_holds():
    rec = _read(
        [
            {"id": "stock", "type": "system", "state": True},
            {"id": "stockAvailableFifo", "type": "system", "state": "yes"},
            {"id": "creditLimit", "type": "system", "state": True},
        ]
    )
    assert rec["holds"] == []
    assert len(rec["trafficLights"]) == 3


def test_credit_limit_block_is_a_hold():
    rec = _read([{"id": "creditLimit", "type": "system", "state": False}])
    assert [h["type"] for h in rec["holds"]] == ["creditLimit"]


def test_no_lights_is_empty_not_error():
    rec = _read([])
    assert rec["holds"] == []
    assert rec["trafficLights"] == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
