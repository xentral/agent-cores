"""Payment, shop references and channel are writable on a sales order.

These four were readable, three of them filterable, and none of them writable —
so an order imported from a marketplace could be found but never labelled with
where it came from, and payment terms could not be set at all. The playbook
recorded that as "changeable only in the UI", which was wrong: v3 takes
`financials.paymentMethod`, `financials.paymentTerms`, `externalOrderNumber`,
`externalOrderId`, `transactionNumber` and `salesChannel` on create AND update.
The adapter simply never mapped them — `payment` was missing from `_WRITABLE`,
right next to `shipping`, which was in it.

Verified live against mvp (create with 45/5/9 against a customer defaulting to
14/2/10, then PATCH to 60 — read back both times).

The partial-update shape is the part worth pinning: sending only
`terms.dueDays` must emit only `paymentTargetDays`, because paymentTerms is a
whole object upstream and echoing an unset key as null would clear a discount
the merchant never touched.
"""

from __future__ import annotations

import pytest
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


@pytest.fixture
def adapter() -> SalesOrderAdapter:
    return SalesOrderAdapter()


@pytest.mark.parametrize("creating", [True, False])
def test_payment_method_and_terms_reach_the_v3_financials_block(adapter, creating):
    v3, rejected = adapter.map_write(
        {
            "payment": {
                "method": "paym_23",
                "terms": {"dueDays": 30, "discountPercent": 3, "discountDays": 7},
            }
        },
        creating=creating,
    )
    assert v3["financials"]["paymentMethod"] == {"id": "23"}
    assert v3["financials"]["paymentTerms"] == {
        "paymentTargetDays": 30,
        "paymentTargetDiscount": 3,
        "paymentTargetDiscountDays": 7,
    }
    assert not rejected


def test_a_partial_terms_update_emits_only_what_was_set(adapter):
    """Upstream paymentTerms is one object — a key we did not receive must not be
    sent, or a PATCH of the due days would silently wipe the cash discount."""
    v3, _ = adapter.map_write({"payment": {"terms": {"dueDays": 60}}}, creating=False)
    assert v3["financials"]["paymentTerms"] == {"paymentTargetDays": 60}


def test_currency_and_payment_share_the_financials_block(adapter):
    """Both map into `financials`; the second must not clobber the first."""
    v3, _ = adapter.map_write({"currency": "EUR", "payment": {"method": "paym_1"}}, creating=True)
    assert v3["financials"]["currency"] == "EUR"
    assert v3["financials"]["paymentMethod"] == {"id": "1"}


def test_payment_status_is_reported_rather_than_dropped(adapter):
    """It is derived upstream. Echoing null back (read-modify-write) is a no-op;
    trying to set it must be reported, not silently ignored."""
    _, rejected = adapter.map_write({"payment": {"status": "paid"}}, creating=False)
    assert "payment.status" in rejected

    _, rejected = adapter.map_write({"payment": {"status": None}}, creating=False)
    assert not rejected


def test_shop_and_marketplace_references_are_writable(adapter):
    v3, rejected = adapter.map_write(
        {
            "references": {
                "externalNumber": "AMZ-302-1",
                "externalId": "EXT-9",
                "paymentTransactionId": "TXN-7",
                "customerOrderNumber": "PO-1",
            }
        },
        creating=True,
    )
    assert v3["externalOrderNumber"] == "AMZ-302-1"
    assert v3["externalOrderId"] == "EXT-9"
    assert v3["transactionNumber"] == "TXN-7"
    assert v3["customerOrderNumber"] == "PO-1"
    assert not rejected


def test_channel_maps_to_sales_channel(adapter):
    v3, rejected = adapter.map_write({"channel": "ch_5"}, creating=True)
    assert v3["salesChannel"] == {"id": "5"}
    assert not rejected


def test_the_fields_are_declared_writable_in_the_schema(adapter):
    """map_write is only half of it — the schema has to advertise them, or no
    caller (and no agent reading `describe`) ever tries."""
    meta = adapter.metadata(None)
    props = meta["rootNode"]["properties"]

    payment = (props["payment"].get("node") or props["payment"])["properties"]
    assert payment["method"].get("creatable") and payment["method"].get("updatable")
    terms = (payment["terms"].get("node") or payment["terms"])["properties"]
    assert terms["dueDays"].get("creatable") and terms["dueDays"].get("updatable")
    assert payment["status"].get("access") == "readOnly"

    refs = (props["references"].get("node") or props["references"])["properties"]
    for key in ("externalNumber", "externalId"):
        assert refs[key].get("creatable") and refs[key].get("updatable"), key
