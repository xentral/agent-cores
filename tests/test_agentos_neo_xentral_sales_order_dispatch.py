"""SalesOrder dispatch: hand the order to logistics (Autoversand).

The v1 dispatch action (POST /api/v1/salesOrders/{id}/actions/dispatch) creates a
pick run + delivery note and starts shipping. It is wired as a `fulfillment`
process-step command routed through action_map, so it runs via
`run op=dispatch` like the other order actions.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


def test_dispatch_routes_to_v1_dispatch_action():
    route = SalesOrderAdapter().action_map["dispatch"]
    assert route["method"] == "POST"
    assert route["path"] == "/api/v1/salesOrders/{id}/actions/dispatch"


def test_dispatch_is_a_fulfillment_step_and_flagged_destructive():
    groups = {g["key"]: g for g in SalesOrderAdapter().steps()}
    assert "fulfillment" in groups
    cmd = {c["key"]: c for c in groups["fulfillment"]["commands"]}["dispatch"]
    assert cmd["destructive"] is True
    assert "logistics" in cmd["description"].lower()
    # optional printPickList command param is advertised
    assert "printPickList" in cmd["command"]["properties"]


def test_document_status_steps_unchanged():
    groups = {g["key"]: g for g in SalesOrderAdapter().steps()}
    keys = {c["key"] for c in groups["documentStatus"]["commands"]}
    assert keys == {"release", "close", "cancel"}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
