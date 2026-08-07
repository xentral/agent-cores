"""What the customer decides for its orders is readable again.

Eight fields under ``defaults``/``finance`` were hardcoded to ``null`` in the
adapter, so credit limit, payment terms, taxation and the default carrier read as
empty for every customer on every tenant — including customers whose orders
demonstrably carry a payment method and terms. Measured on mvp: all null across
the whole customer base, on ``get`` as well as ``list``. That reads like "this
tenant configures nothing", which is a very different statement from "we do not
map it", and it is the one a workflow would branch on.

Four of them are on the v3 customer resource and are now mapped:
``financials.paymentTerms``, ``financials.tax.taxation``,
``fulfillment.shippingMethod`` and ``financials.creditLimit``.

Three genuinely are not, and stay null on purpose: ``priceList`` and
``partialShipping`` have no slot on this resource, and ``openAmount`` is a
computed A/R figure rather than a customer field.

Driven from a synthetic v3 payload rather than a live record: the point is the
mapping, and hunting for a tenant whose customer happens to carry a credit limit
would make the test pass or fail for reasons that have nothing to do with it.
"""

from __future__ import annotations

import pytest
from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter

V3_CUSTOMER = {
    "id": "42",
    "number": "10001",
    "communication": {"language": "DE"},
    "financials": {
        "defaultCurrency": "EUR",
        "creditLimit": 25000,
        "paymentMethod": {"id": "23"},
        "paymentTerms": {
            "paymentTargetDays": 30,
            "paymentTargetDiscount": 2.5,
            "paymentTargetDiscountDays": 10,
        },
        "tax": {"taxation": "eu"},
    },
    "fulfillment": {"shippingMethod": {"id": "7"}, "deliveryBlock": True},
    "deviatingDebtorAccountNumber": "711999",
}


@pytest.fixture
def record() -> dict:
    return CustomerAdapter().map_read(dict(V3_CUSTOMER))


def test_payment_terms_are_readable(record):
    assert record["defaults"]["paymentTerms"] == {
        "dueDays": 30,
        "discountPercent": 2.5,
        "discountDays": 10,
    }


def test_taxation_and_shipping_method_are_readable(record):
    assert record["defaults"]["taxation"] == "eu"
    assert record["defaults"]["shippingMethod"]["id"] == "ship_7"


def test_credit_limit_is_a_money_pair_in_the_document_currency(record):
    """The model states money as {amount, currency} (ADR-006); v3 sends a bare
    number, so the customer's default currency has to be carried over or the
    figure is unusable for a credit check."""
    assert record["finance"]["creditLimit"] == {"amount": "25000.00", "currency": "EUR"}


def test_hold_and_debtor_account_still_map(record):
    assert record["finance"]["onHold"] is True
    assert record["finance"]["debtorAccountNumber"] == "711999"


def test_the_three_fields_with_no_upstream_slot_stay_null(record):
    """Not an oversight — asserted so a later "fix" cannot quietly invent them."""
    assert record["defaults"]["priceList"] is None
    assert record["defaults"]["partialShipping"] is None
    assert record["finance"]["openAmount"] is None


def test_an_empty_upstream_payload_does_not_raise(record):
    """Most customers carry none of this; the mapping must degrade to nulls."""
    empty = CustomerAdapter().map_read({"id": "1", "number": "2"})
    assert empty["defaults"]["paymentTerms"] == {
        "dueDays": None,
        "discountPercent": None,
        "discountDays": None,
    }
    assert empty["defaults"]["taxation"] is None
    assert empty["defaults"]["shippingMethod"] is None
    assert empty["finance"]["creditLimit"] is None
