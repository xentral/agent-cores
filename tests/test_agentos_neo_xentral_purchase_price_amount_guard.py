"""A non-numeric purchase-price amount is refused, not raised.

The v2 PATCH schema wants the amount as a NUMBER (create wants a string), so the
adapter converted with a bare `float()`. Anything unparsable raised straight out
of `map_write` and took the whole request down — the caller got a stack trace
instead of a field name.

Found the hard way: an earlier verify run wrote its `·vt` marker into a price
field (upstream accepted it there), and every later run then died on read-back
with `could not convert string to float: '4.00·vt'`, leaving PurchasePrice absent
from the manifest entirely.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_price import PurchasePriceAdapter


@pytest.mark.parametrize("amount", ["4.00·vt", "abc", "12,50", "", "1.2.3"])
def test_unparsable_amount_is_rejected_on_update(amount: str) -> None:
    body, rejected = PurchasePriceAdapter().map_write(
        {"unitPrice": {"amount": amount}}, creating=False
    )
    assert "unitPrice.amount" in rejected
    assert "price" not in body


def test_valid_amount_still_goes_out_as_a_number_on_update() -> None:
    """The reason the conversion exists: v2 PATCH rejects a string amount."""
    body, rejected = PurchasePriceAdapter().map_write(
        {"unitPrice": {"amount": "4.00", "currency": "EUR"}}, creating=False
    )
    assert rejected == set()
    assert body["price"] == {"amount": 4.0, "currency": "EUR"}


def test_create_still_sends_a_string_amount() -> None:
    """…and create wants the opposite. The guard must not flatten that difference."""
    body, rejected = PurchasePriceAdapter().map_write(
        {"product": "prd_1", "unitPrice": {"amount": "4.00", "currency": "EUR"}}, creating=True
    )
    assert rejected == set()
    assert body["price"]["amount"] == "4.00"


def test_a_bad_amount_does_not_swallow_the_rest_of_the_write() -> None:
    """Only the offending field is refused; the caller learns which one."""
    _body, rejected = PurchasePriceAdapter().map_write(
        {"unitPrice": {"amount": "nope"}, "remark": "keep me"}, creating=False
    )
    assert rejected == {"unitPrice.amount"}
