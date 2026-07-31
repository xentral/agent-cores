"""Customer carries what a migration needs, and refuses instead of dropping.

A 6k-record CRM import surfaced four gaps at once, all with the same root: fields the
merchant sends were listed in `_IGNORE`, so a create answered 201 with the value
discarded and nothing to see. On a migration that destroys every foreign key silently.
ADR-014 is explicit — a write naming a non-writable field answers 409 with the field
list, and `_IGNORE` may hold only envelope keys a write can never mean.

All four fields are plain v3 pass-through; verified on mvp:
  * POST /api/v3/customers with `number` stores it verbatim (omit it → number range)
  * `notes` (the "Sonstiges" CRM remark) is writable
  * customFields write as {key, label, value}; `label` is required upstream
  * contactPersons carry position / department / subDepartment / remarks separately
"""

from __future__ import annotations

from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter


def _f() -> dict[str, Any]:
    return CustomerAdapter().fields()


# ---- the silent drop ----------------------------------------------------


def test_ignore_holds_only_envelope_keys():
    """The regression that started this: a business field parked in _IGNORE is
    accepted and thrown away. Only keys a write can never mean belong there."""
    assert CustomerAdapter._IGNORE == {"object", "id", "createdAt", "updatedAt"}
    assert SupplierAdapter._IGNORE >= {"object", "id"}


def test_non_writable_fields_are_reported_not_swallowed():
    for field, value in (
        ("status", "archived"),
        ("finance", {"creditLimit": 100}),
        ("parent", {"id": "cus_1"}),
        ("billTo", {"id": "cus_1"}),
        ("channels", []),
    ):
        rejected = CustomerAdapter().map_write({field: value}, creating=True)[1]
        assert field in rejected, f"{field} must surface as a wish, not vanish"


def test_read_emitted_envelope_round_trips_quietly():
    quiet = {"object": "customer", "id": "cus_7", "createdAt": "x", "updatedAt": "y"}
    assert CustomerAdapter().map_write(quiet, creating=False)[1] == set()


# ---- number: the migration key ------------------------------------------


def test_number_is_creatable_and_filterable():
    n = _f()["number"]
    assert n.get("creatable") is True
    assert n.get("filterable") is True  # the key a repeated import matches on
    assert n.get("access") != "readOnly"


def test_number_is_sent_on_create():
    body, rejected = CustomerAdapter().map_write(
        {"name": "Acme", "number": "K-10023"}, creating=True
    )
    assert body["number"] == "K-10023"
    assert rejected == set()


def test_number_on_update_is_refused_rather_than_dropped():
    """v3 takes a number on POST but not on PATCH. Silently ignoring it would leave
    the caller believing a correction landed."""
    body, rejected = CustomerAdapter().map_write({"number": "K-1"}, creating=False)
    assert "number" not in body
    assert rejected == {"number"}


# ---- notes ---------------------------------------------------------------


def test_notes_round_trip():
    assert _f()["notes"].get("creatable") is True
    body, _ = CustomerAdapter().map_write({"notes": "Kunde seit 2019"}, creating=True)
    assert body["notes"] == "Kunde seit 2019"
    read = CustomerAdapter().map_read({"id": 1, "notes": "aus Close"})
    assert read["notes"] == "aus Close"


# ---- customFields --------------------------------------------------------


def test_custom_fields_are_typed_so_an_agent_can_read_the_shape():
    cf = _f()["customFields"]
    assert cf["type"] == "collection"
    assert set(cf["node"]["properties"]) == {"key", "label", "type", "value"}


def test_custom_fields_write_shape():
    body, rejected = CustomerAdapter().map_write(
        {"customFields": [{"key": "customField6", "label": "Close-ID", "value": "lead-9"}]},
        creating=True,
    )
    assert body["customFields"] == [{"key": "customField6", "label": "Close-ID", "value": "lead-9"}]
    assert rejected == set()


def test_custom_field_without_label_is_refused_not_sent():
    """Upstream requires label and answers 400. Refusing here names the field the
    caller can act on instead of forwarding an error they cannot map back."""
    _body, rejected = CustomerAdapter().map_write(
        {"customFields": [{"key": "customField6", "value": "x"}]}, creating=True
    )
    assert rejected == {"customFields"}


def test_custom_fields_are_read_from_the_include():
    read = CustomerAdapter().map_read(
        {
            "id": 1,
            "customFields": [
                {"key": "customField6", "label": "Close-ID", "type": "string", "value": "lead-9"}
            ],
        }
    )
    assert read["customFields"] == [
        {"key": "customField6", "label": "Close-ID", "type": "string", "value": "lead-9"}
    ]


def test_custom_fields_are_actually_requested_upstream():
    # without the include the values are absent from the payload, not empty
    assert "customFields" in CustomerAdapter.include


# ---- contacts ------------------------------------------------------------


def test_contact_keeps_position_department_and_remarks_apart():
    """`role` used to be the only slot: it carried department, position was dropped
    entirely, and the remark had nowhere to go."""
    props = _f()["contacts"]["node"]["properties"]
    for key in ("position", "department", "subDepartment", "remarks", "internalNote"):
        assert key in props, key
        assert props[key].get("creatable") is True, key
    assert "role" not in props


def test_contact_maps_the_details_sub_object_both_ways():
    from xentral_entity_cores.agentos_neo_xentral.emulated.partner_subresources import (
        contact_from_v3,
        contact_to_v3,
    )

    v3 = {
        "id": 10,
        "name": "Max Muster",
        "department": "Einkauf",
        "subDepartment": "Rohstoffe",
        "contactPersonDetails": {
            "position": "Leiter",
            "remarks": "Notiz aus Close",
            "internalNote": "intern",
            "language": "DE",
        },
    }
    m = contact_from_v3(v3)
    assert (m["department"], m["position"], m["remarks"]) == (
        "Einkauf",
        "Leiter",
        "Notiz aus Close",
    )
    assert m["subDepartment"] == "Rohstoffe" and m["internalNote"] == "intern"

    out = contact_to_v3(m)
    assert out["department"] == "Einkauf"
    assert out["contactPersonDetails"]["position"] == "Leiter"
    assert out["contactPersonDetails"]["remarks"] == "Notiz aus Close"


def test_contact_without_detail_fields_sends_no_empty_sub_object():
    from xentral_entity_cores.agentos_neo_xentral.emulated.partner_subresources import (
        contact_to_v3,
    )

    assert "contactPersonDetails" not in contact_to_v3({"name": "Nur Name"})


# ---- delete --------------------------------------------------------------


def test_customer_can_be_deleted():
    """Create-but-cannot-clean-up leaves test records in a live tenant. v3 has no
    delete, v1 does."""
    assert "delete" in CustomerAdapter.manifest.operations
    assert CustomerAdapter._V1_PATH == "/api/v1/customers"


# ---- the gap that stays a gap -------------------------------------------


def test_lead_flag_is_read_only_and_carried_as_a_wish():
    """No v3 customers payload exposes the lead flag, so `type` must not claim to be
    writable — and the gap has to stay visible rather than disappear."""
    import json
    import pathlib

    assert _f()["type"]["access"] == "readOnly"
    prio = json.loads(
        (
            pathlib.Path(__file__).parent.parent / "cores/agentos_neo_xentral/priorities.json"
        ).read_text(encoding="utf-8")
    )
    wishes = [w for w in prio["entities"]["Customer"] if w["field"] == "type"]
    assert wishes and set(wishes[0]["ops"]) == {"create", "update"}
