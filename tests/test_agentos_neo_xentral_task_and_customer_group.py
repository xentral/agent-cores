"""Task and CustomerGroup: the two entities that exist only on the entity API.

Both are new surfaces rather than remappings, so what these tests pin is the
translation between Xentral's vocabulary and the model's — and, more importantly,
the two places where the model deliberately refuses to pretend:

* a task's `assignee` is read-only upstream. It answers 2xx and stays null, so
  the core refuses the write instead of letting a caller believe the task was
  handed over.
* `customerGroup` is ONE upstream table holding three business objects
  (`group` / `price_group` / `representative`), and the conditions block applies
  to only two of them — enforced upstream, field by field.
"""

from __future__ import annotations

import json

from xentral_entity_cores.agentos_neo_xentral.emulated.customer_group import (
    CustomerGroupAdapter,
)
from xentral_entity_cores.agentos_neo_xentral.emulated.task import TaskAdapter


# --------------------------------------------------------------------- Task
def test_status_and_recurrence_speak_the_model_not_the_wire():
    d = TaskAdapter().map_read(
        {"uuid": "u1", "title": "T", "status": "in_progress", "recurrenceInterval": "one_time"}
    )
    assert d["status"] == "inProgress"
    assert d["recurrence"]["interval"] == "once"


def test_writing_status_translates_back():
    wire, _ = TaskAdapter().map_write(
        {"status": "inProgress", "recurrence": {"interval": "once"}}, creating=False
    )
    assert wire["status"] == "in_progress"
    assert wire["recurrenceInterval"] == "one_time"


def test_the_assignee_cannot_be_written():
    """Upstream answers 2xx and leaves it null — that is worse than an error,
    because the caller believes the task was assigned."""
    spec = TaskAdapter().fields()["assignee"]
    assert not spec.get("creatable") and not spec.get("updatable")
    wire, rejected = TaskAdapter().map_write(
        {"title": "T", "assignee": {"id": "emp_1"}}, creating=True
    )
    assert "assignee" not in wire
    assert "assignee" in rejected


def test_the_completion_date_is_not_writable_either():
    """Writing `status: completed` does not stamp it, so neither do we."""
    _wire, rejected = TaskAdapter().map_write(
        {"dates": {"completed": "2026-01-01"}}, creating=False
    )
    assert "dates.completed" in rejected


def test_every_queryable_nested_path_has_an_upstream_alias():
    """Without the alias the facade forwards the MODEL path and upstream answers
    422 "Property 'dates.created' does not exist" — five filters and two sorts
    failed exactly this way before the table was completed."""
    a = TaskAdapter()

    def queryable(props, prefix=""):
        out = []
        for name, spec in props.items():
            sub = spec.get("properties")
            if sub:
                out += queryable(sub, f"{prefix}{name}.")
            elif spec.get("filterable") or spec.get("sortable"):
                out.append(f"{prefix}{name}")
        return out

    nested = [p for p in queryable(a.fields()) if "." in p]
    assert nested, "the model has nested queryable paths"
    missing = [p for p in nested if p not in a.query_aliases]
    assert not missing, f"no upstream alias for {missing}"


def test_a_task_is_addressed_by_uuid():
    assert TaskAdapter()._created_handle({"data": {"id": "9", "uuid": "u-x"}}) == "u-x"
    assert TaskAdapter().map_read({"id": "9", "uuid": "u-x", "title": "T"})["id"] == "tsk_u-x"


# ------------------------------------------------------------- CustomerGroup
def test_the_three_kinds_are_named_not_collapsed():
    m = CustomerGroupAdapter().map_read
    assert m({"uuid": "u", "type": "group"})["kind"] == "customerGroup"
    assert m({"uuid": "u", "type": "price_group"})["kind"] == "priceGroup"
    assert m({"uuid": "u", "type": "representative"})["kind"] == "salesRepresentative"


def test_free_shipping_active_is_a_flag_not_a_decimal():
    """Upstream declares and stores it as a decimal holding 0 or 1."""
    m = CustomerGroupAdapter().map_read
    assert m({"uuid": "u", "isFreeShippingActive": "1.00"})["conditions"]["freeShippingActive"]
    assert (
        m({"uuid": "u", "isFreeShippingActive": "0.00"})["conditions"]["freeShippingActive"]
        is False
    )
    assert m({"uuid": "u"})["conditions"]["freeShippingActive"] is None
    wire, _ = CustomerGroupAdapter().map_write(
        {"conditions": {"freeShippingActive": True}}, creating=False
    )
    assert wire["isFreeShippingActive"] == 1


def test_conditions_on_a_plain_customer_group_are_refused():
    """Upstream rejects each one with "not applicable for the group type"; naming
    the whole set at once beats failing on whichever comes first."""
    _wire, rejected = CustomerGroupAdapter().map_write(
        {
            "name": "G",
            "kind": "customerGroup",
            "conditions": {"baseDiscount": "5.00", "paymentTermDays": 30},
        },
        creating=True,
    )
    assert rejected == {"conditions.baseDiscount", "conditions.paymentTermDays"}


def test_a_price_group_may_carry_the_same_conditions():
    wire, rejected = CustomerGroupAdapter().map_write(
        {"kind": "priceGroup", "conditions": {"baseDiscount": "5.00", "paymentTermDays": 30}},
        creating=True,
    )
    assert not rejected
    assert wire["baseDiscount"] == "5.00"
    assert wire["paymentTermDays"] == 30
    assert wire["type"] == "price_group"


def test_the_code_is_the_short_code_a_clerk_types():
    d = CustomerGroupAdapter().map_read({"uuid": "u", "identificationNumber": "VETRL"})
    assert d["code"] == "VETRL"
    wire, _ = CustomerGroupAdapter().map_write({"code": "VETRL"}, creating=True)
    assert wire["identificationNumber"] == "VETRL"


def test_both_entities_are_registered_in_the_core():
    from xentral_entity_cores.agentos_neo_xentral import CORE

    keys = {a.manifest.key for a in CORE.adapters}
    assert {"Task", "CustomerGroup"} <= keys


def test_the_probe_bumps_a_wall_clock_instead_of_marking_it():
    """The generic marker turns "00:00:00" into a value upstream accepts with a
    200 and normalises straight back to midnight — a writable field then reads as
    "did not persist" forever."""
    from xentral_entity_cores.agentos_neo_xentral.checks.verify import _time_string_toggle

    assert _time_string_toggle("00:00:00") == "01:00:00"
    assert _time_string_toggle("23:59:59") == "00:59:59"
    assert _time_string_toggle("14:30") == "15:30:00"
    assert _time_string_toggle("not a time") is None
    assert _time_string_toggle(None) is None
    assert json.dumps(_time_string_toggle("00:00:00"))  # plain JSON, no marker
