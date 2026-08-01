"""The verify probe measures line items instead of skipping them.

`_update_targets` walks scalar leaves and stops at a collection — correct, since a
document's positions are not written by PATCHing the whole list; the core
reconciles them one line at a time against the v3 lineItems sub-resource. But
stopping there meant no line field was ever measured, so all six read `offen` on
every sales document. Including `items.purchasePrice`, the field the suite was
extended for: declared creatable on all four types and never once sent.

These pin the toggles the per-line probe builds. Each has to be net-zero — the
suite runs against a live tenant — so a value it cannot restore is one it must not
touch.
"""

from __future__ import annotations

from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.checks.verify import _line_eq, _line_toggle

_NO_SPEC: dict[str, Any] = {}


def test_money_moves_by_one_and_keeps_the_currency() -> None:
    test, restore = _line_toggle(_NO_SPEC, "unitPrice", {"amount": "9.90", "currency": "EUR"})
    assert test == {"amount": "10.90", "currency": "EUR"}
    assert restore == {"amount": "9.90", "currency": "EUR"}


def test_a_quantity_keeps_its_unit() -> None:
    test, restore = _line_toggle(_NO_SPEC, "quantity", {"value": 3, "unit": "piece"})
    assert test == {"value": 4.0, "unit": "piece"}
    assert restore == {"value": 3.0, "unit": "piece"}


def test_a_number_moves_by_one() -> None:
    assert _line_toggle(_NO_SPEC, "discountPercent", 5) == (6, 5)


def test_text_toggles_with_the_marker_and_back() -> None:
    test, restore = _line_toggle(_NO_SPEC, "description", "German books")
    assert test == "German books·vt"
    assert restore == "German books"


def test_a_select_toggles_to_another_declared_option() -> None:
    spec = {"options": [{"value": "standard"}, {"value": "reduced"}]}
    assert _line_toggle(spec, "taxRate", "standard") == ("reduced", "standard")


@pytest.mark.parametrize(
    "orig",
    [
        None,  # nothing to restore to
        "",  # upstream ignores an empty-string write, so the restore would not land
        {"amount": None, "currency": "EUR"},
        {"value": None, "unit": "piece"},
    ],
)
def test_a_value_that_cannot_be_restored_is_not_probed(orig: Any) -> None:
    """Better an `offen` cell than a line left changed on a live tenant."""
    assert _line_toggle(_NO_SPEC, "x", orig) == (None, None)


def test_a_money_string_from_the_wire_compares_equal() -> None:
    """Upstream answers "10.90000000" to a "10.90" write."""
    assert _line_eq({"amount": "10.90000000", "currency": "EUR"}, {"amount": "10.90"})


def test_a_quantity_compares_numerically() -> None:
    assert _line_eq({"value": 4, "unit": "piece"}, {"value": 4.0, "unit": "piece"})


def test_a_different_value_does_not_compare_equal() -> None:
    """The check that made items.taxRate red: sent "reduced", read back "standard"."""
    assert not _line_eq("standard", "reduced")
    assert not _line_eq({"amount": "9.90"}, {"amount": "10.90"})
