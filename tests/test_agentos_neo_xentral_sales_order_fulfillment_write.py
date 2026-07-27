"""SalesOrder fulfilment policy: auto-dispatch and priority are writable.

`fulfillmentPolicy.auto` is the neutral face of v3 `autoDispatch` — the "Autoversand"
checkbox. It was read-only in this adapter until the v3 create/update DTOs gained the
field; a probe against mvp on 2026-07-27 confirmed both DTOs persist it (written,
read back, flipped, read back again — with a control field proving the probe path).
`priority` is the same story for the boolean `fastLane`.

These pin:
  * the neutral → v3 mapping in both directions of the boolean, and the
    select ↔ boolean translation for priority (only "high" means fastLane);
  * that a merchant can flip the flag OFF, not just on — `False` must reach the
    payload, so the mapping may not use truthiness to decide what to send;
  * that `partialShipping` stays rejected: the read hardcodes "allowed" and there is
    no upstream slot, so silently swallowing a write would be a lie.
"""

from __future__ import annotations

from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


def _write(model: dict[str, Any], *, creating: bool = False):
    return SalesOrderAdapter().map_write(model, creating=creating)


def test_auto_dispatch_maps_to_v3_auto_dispatch():
    v3, rejected = _write({"fulfillmentPolicy": {"auto": True}})
    assert rejected == set()
    assert v3 == {"autoDispatch": True}


def test_auto_dispatch_can_be_turned_off():
    # The regression that truthiness-based mapping would cause: False silently
    # dropped, so the merchant can switch auto-shipping on but never off again.
    v3, rejected = _write({"fulfillmentPolicy": {"auto": False}})
    assert rejected == set()
    assert v3 == {"autoDispatch": False}


def test_auto_dispatch_writable_on_create_too():
    v3, rejected = _write(
        {"customer": {"id": "cus_20201"}, "fulfillmentPolicy": {"auto": True}}, creating=True
    )
    assert rejected == set()
    assert v3["autoDispatch"] is True
    assert v3["address"] == {"id": "20201"}


def test_priority_translates_to_fast_lane_boolean():
    assert _write({"fulfillmentPolicy": {"priority": "high"}})[0] == {"fastLane": True}
    assert _write({"fulfillmentPolicy": {"priority": "normal"}})[0] == {"fastLane": False}


def test_partial_shipping_change_is_rejected_not_swallowed():
    v3, rejected = _write({"fulfillmentPolicy": {"partialShipping": "forbidden"}})
    assert rejected == {"fulfillmentPolicy.partialShipping"}
    assert "partialShipping" not in v3


def test_partial_shipping_rejection_survives_a_mixed_write():
    # A 409 on the whole write is the contract (base._write); the writable siblings
    # must still be mapped so the error payload is not the only signal.
    v3, rejected = _write(
        {"fulfillmentPolicy": {"auto": True, "priority": "high", "partialShipping": "forbidden"}}
    )
    assert rejected == {"fulfillmentPolicy.partialShipping"}
    assert v3 == {"autoDispatch": True, "fastLane": True}


def test_round_trip_write_is_not_a_409():
    # The read emits partialShipping unconditionally, so a read-modify-write client
    # echoes it back on every PATCH. Echoing the unchanged value must be a no-op —
    # otherwise turning auto-dispatch off via a full-model write is impossible.
    read_back = SalesOrderAdapter().map_read({"id": 1, "autoDispatch": True})
    policy = read_back["fulfillmentPolicy"]
    policy["auto"] = False  # the merchant unticks "Autoversand", everything else as read

    v3, rejected = _write({"fulfillmentPolicy": policy})
    assert rejected == set()
    assert v3 == {"autoDispatch": False, "fastLane": False}


def test_schema_advertises_the_new_write_flags():
    props = SalesOrderAdapter().fields()["fulfillmentPolicy"]["properties"]
    assert props["auto"]["creatable"] and props["auto"]["updatable"]
    assert props["priority"]["creatable"] and props["priority"]["updatable"]
    assert [o["value"] for o in props["priority"]["options"]] == ["normal", "high"]
    # partialShipping must NOT advertise itself as writable while it is rejected
    assert not props["partialShipping"].get("creatable")
    assert not props["partialShipping"].get("updatable")
