"""Regression: reference filter values must be stripped to the bare numeric id.

A model filter like ``customer=cus_20423`` must reach the upstream as
``address.id`` (query_alias) with value ``20423`` — the upstream filters on the
numeric id, so a leftover ``cus_`` prefix silently returns the wrong rows. Only
reference-typed filter keys are stripped; string/enum filters (number, tags,
status, customerOrderNumber) are left untouched.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter


def _q(*pairs):
    """Build a filter[i][key/op/value] query param list."""
    out = []
    for i, (key, op, value) in enumerate(pairs):
        out += [
            (f"filter[{i}][key]", key),
            (f"filter[{i}][op]", op),
            (f"filter[{i}][value]", value),
        ]
    return out


def test_reference_filter_keys_detected():
    keys = DeliveryNoteAdapter()._reference_filter_keys()
    # reference fields (top-level and nested) are found
    assert {"customer", "project", "channel", "items.product"} <= keys
    # scalar/enum/string fields are NOT reference keys
    assert "number" not in keys
    assert "status" not in keys
    assert "references.customerOrderNumber" not in keys


def test_reference_value_prefix_stripped():
    params = _q(("customer", "equals", "cus_20423"))
    out = dict(DeliveryNoteAdapter()._strip_reference_filter_prefixes(params))
    assert out["filter[0][value]"] == "20423"  # prefix gone
    assert out["filter[0][key]"] == "customer"  # key untouched (alias runs later)
    assert out["filter[0][op]"] == "equals"


def test_string_and_enum_filters_untouched():
    params = _q(
        ("number", "equals", "300000"),
        ("status", "equals", "draft"),
        ("references.customerOrderNumber", "equals", "CLAUDE-DN-001"),
        ("tags", "equals", "claude-e2e-test"),
    )
    out = dict(DeliveryNoteAdapter()._strip_reference_filter_prefixes(params))
    assert out["filter[0][value]"] == "300000"
    assert out["filter[1][value]"] == "draft"
    assert out["filter[2][value]"] == "CLAUDE-DN-001"
    assert out["filter[3][value]"] == "claude-e2e-test"


def test_reference_value_without_prefix_is_left_as_is():
    # already-numeric reference value must not be mangled
    params = _q(("customer", "equals", "4"))
    out = dict(DeliveryNoteAdapter()._strip_reference_filter_prefixes(params))
    assert out["filter[0][value]"] == "4"


def test_nested_reference_filter_stripped():
    params = _q(("items.product", "equals", "prd_61617"))
    out = dict(DeliveryNoteAdapter()._strip_reference_filter_prefixes(params))
    assert out["filter[0][value]"] == "61617"
