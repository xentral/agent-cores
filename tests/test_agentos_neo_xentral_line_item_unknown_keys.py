"""Unknown line-item keys surface as a wish instead of vanishing.

`_item_to_v3` picks the keys it knows and ignores the rest, so an unsupported item
sub-key used to disappear without a trace — a create carrying it came back 201 with
the value never written (that is exactly how the missing purchasePrice presented).
Top-level keys have always been reported through `rejected`; these pin that item
sub-keys are now held to the same standard, and that the schema is the allowlist so
a read-modify-write round-trip stays quiet.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

_ALL = (QuoteAdapter, SalesOrderAdapter, SalesInvoiceAdapter, CreditNoteAdapter)

_LINE: dict[str, Any] = {"product": {"id": "prd_1"}, "quantity": {"value": 1}}


def _rejected(cls: type, item: dict[str, Any], *, creating: bool = True) -> set[str]:
    return cls().map_write({"items": [item]}, creating=creating)[1]


def test_unknown_item_key_is_reported():
    for cls in _ALL:
        rej = _rejected(cls, {**_LINE, "serialNumbers": ["SN-1"]})
        assert "items.serialNumbers" in rej, cls.__name__


def test_known_item_keys_are_not_reported():
    for cls in _ALL:
        rej = _rejected(cls, {**_LINE, "description": "x", "taxRate": "standard"})
        assert not {r for r in rej if r.startswith("items.")}, cls.__name__


def test_read_emitted_keys_survive_a_round_trip_quietly():
    """Reading a record and writing it back must not spray wishes: every key the
    read emits is declared in the schema, even the read-only ones."""
    raw = {
        "id": 1,
        "financials": {"currency": "EUR"},
        "lineItems": [
            {
                "id": 151010,
                "order": 1,
                "product": {"id": "61988"},
                "quantity": 2,
                "unit": "piece",
                "price": {"net": {"amount": "10.00", "currency": "EUR"}},
                "discount": 5,
                "taxRate": "standard",
                "purchasePrice": {"net": {"amount": "3.33", "currency": "EUR"}},
            }
        ],
    }
    for cls in _ALL:
        a = cls()
        item = a.map_read(raw)["items"][0]
        rej = {
            r for r in a.map_write({"items": [item]}, creating=True)[1] if r.startswith("items.")
        }
        assert rej == set(), f"{cls.__name__}: {rej}"


def test_purchase_price_would_have_been_caught():
    """The regression this exists for: before the field was modelled, an EK on a
    line item was accepted and silently dropped. It is modelled now, so it must NOT
    be rejected — but an EK typo'd one level off still is."""
    for cls in _ALL:
        assert "items.purchasePrice" not in _rejected(
            cls, {**_LINE, "purchasePrice": {"amount": 1, "currency": "EUR"}}
        ), cls.__name__
        assert "items.purchase_price" in _rejected(cls, {**_LINE, "purchase_price": 1}), (
            cls.__name__
        )


def test_reported_on_update_too_where_items_are_processed():
    # salesOrder reconciles items on UPDATE, so unknown keys must surface there as well
    assert "items.serialNumbers" in _rejected(
        SalesOrderAdapter, {**_LINE, "serialNumbers": ["SN-1"]}, creating=False
    )


def test_salesorder_compose_path_answers_409_not_a_silent_drop():
    """map_write returning the rejection is not enough: salesOrder's UPDATE splits
    `items` off before delegating, and with an items-only body never calls map_write
    at all. The compose path has to answer 409 itself — this is the live behaviour
    that stayed silent while the unit-level map_write check already passed."""
    a = SalesOrderAdapter()
    calls: list = []

    async def _fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        calls.append("get")
        return 200, {"data": {"id": 22932, "financials": {"currency": "EUR"}, "lineItems": []}}

    async def _fake_li_call(method, url, token, al, client, payload=None):  # noqa: ANN001, ANN202
        calls.append(method)
        return 200, {"data": {}}

    a._get = _fake_get  # type: ignore[method-assign]
    a._li_call = _fake_li_call  # type: ignore[method-assign]

    resp = asyncio.run(
        a.request(
            method="PATCH",
            handle="so_22932",
            query=[],
            body=json.dumps(
                {"items": [{"id": "151012", "purchase_price": 9.99, "serialNumbers": ["SN-1"]}]}
            ).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )
    assert resp.status_code == 409, resp.body
    fields = json.loads(resp.content)["fields"]
    assert "items.purchase_price" in fields
    assert "items.serialNumbers" in fields
    # and it refuses BEFORE touching the sub-resource
    assert "POST" not in calls and "PATCH" not in calls and "DELETE" not in calls


def test_non_dict_items_do_not_crash_the_write():
    for cls in _ALL:
        body, rej = cls().map_write({"items": ["nonsense", None, _LINE]}, creating=True)
        assert not {r for r in rej if r.startswith("items.")}, cls.__name__
        assert len(body["lineItems"]) == 1, cls.__name__
