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
  * that `partialShipping` stays rejected — and is no longer read at all. The read used
    to emit a hardcoded "allowed" for every order on every tenant, which is not an empty
    value but a policy statement nobody had checked; the same reasoning that removed
    `defaults.partialShipping` from the customer. Refusing the write was always right;
    claiming to know the answer on the read was not.
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


def test_partial_shipping_is_not_read_at_all():
    """It used to come back "allowed" for every order, whatever the order actually said."""
    record = SalesOrderAdapter().map_read({"id": 1, "autoDispatch": True})
    assert "partialShipping" not in record["fulfillmentPolicy"]


def test_any_partial_shipping_write_is_rejected_not_swallowed():
    """Every mention, not just a change. While the read emitted "allowed", that one value
    had to pass as a no-op so round-tripping clients were not punished for echoing us.
    Nothing echoes it now, so the exception has no reason to exist."""
    for value in ("forbidden", "allowed"):
        v3, rejected = _write({"fulfillmentPolicy": {"partialShipping": value}})
        assert rejected == {"fulfillmentPolicy.partialShipping"}, value
        assert "partialShipping" not in v3, value


def test_partial_shipping_rejection_survives_a_mixed_write():
    # A 409 on the whole write is the contract (base._write); the writable siblings
    # must still be mapped so the error payload is not the only signal.
    v3, rejected = _write(
        {"fulfillmentPolicy": {"auto": True, "priority": "high", "partialShipping": "forbidden"}}
    )
    assert rejected == {"fulfillmentPolicy.partialShipping"}
    assert v3 == {"autoDispatch": True, "fastLane": True}


def test_round_trip_write_is_not_a_409():
    # A read-modify-write client PATCHes back whatever the read gave it. This used to be
    # the reason `partialShipping: "allowed"` had to be tolerated on the write; with the
    # field gone from the read, the round trip is clean without that exception.
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
    # Not read-only — absent. A field described but never writable and never truthfully
    # read is a promise the entity does not keep.
    assert "partialShipping" not in props
