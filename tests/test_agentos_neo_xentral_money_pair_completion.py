"""A currency-only price update completes itself instead of vanishing.

Upstream has no "set the currency" — a money value is an atomic pair. The purchase
price handler spells it out:

    $command->pricePerUnit->has && $command->priceCurrency->has
        && $price->setPricePerUnit($command->pricePerUnit->value, $command->priceCurrency->value);

Measured on mvp: `PATCH price:{currency:"USD"}` answers **400**, while
`price:{amount:4.00, currency:"USD"}` answers 204 and sticks.

The core made that worse. It only emitted the price block when an amount was
present, so a currency-only write went out with **no price at all** — 2xx back,
nothing changed, nothing for the caller to see. That is what the four red
`…currency` cells in the capability manifest meant.

The amount is now filled from the stored record, so the pair leaves complete. It
is the record's own value, not an invented one — the caller's currency still wins.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.price_list import PriceListAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_price import PurchasePriceAdapter


def test_the_pairs_are_declared_where_upstream_couples_them() -> None:
    assert PurchasePriceAdapter.money_pairs == ("unitPrice",)
    assert PriceListAdapter.money_pairs == ("unitPrice",)
    assert ProductAdapter.money_pairs == ("prices.sale", "prices.purchase")


def test_documents_declare_none() -> None:
    """A position has no currency of its own, so there is no pair to complete —
    the header currency is applied unconditionally instead."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

    assert SalesOrderAdapter.money_pairs == ()


class _Adapter(PurchasePriceAdapter):
    """PurchasePrice with its read stubbed, so the completion can be driven
    without a tenant."""

    def __init__(self, stored: dict[str, Any] | None) -> None:
        super().__init__()
        self._stored = stored
        self.reads = 0

    async def _get(self, base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001
        self.reads += 1
        return (200, {"data": self._stored}) if self._stored is not None else (404, {})


def _complete(adapter: _Adapter, model: dict[str, Any]) -> dict[str, Any]:
    asyncio.run(adapter._complete_money_pairs("pp_1", model, "https://x.test", "t", None, None))
    return model


_STORED = {
    "id": "1",
    "product": {"id": "8"},
    "supplier": {"id": "8"},
    "fromQuantity": 1,
    "price": {"amount": "4.00000000", "currency": "EUR"},
}


def test_currency_only_gets_the_stored_amount() -> None:
    a = _Adapter(_STORED)
    model = _complete(a, {"unitPrice": {"currency": "USD"}})
    assert model["unitPrice"]["amount"] == "4.00"
    assert model["unitPrice"]["currency"] == "USD"  # the caller's choice still wins
    assert a.reads == 1


def test_it_then_reaches_the_wire_as_a_complete_pair() -> None:
    """The point of the exercise: previously no price block was emitted at all."""
    a = _Adapter(_STORED)
    model = _complete(a, {"unitPrice": {"currency": "USD"}})
    body, rejected = a.map_write(model, creating=False)
    assert rejected == set()
    assert body["price"] == {"amount": 4.0, "currency": "USD"}


def test_an_explicit_amount_is_not_overwritten() -> None:
    a = _Adapter(_STORED)
    model = _complete(a, {"unitPrice": {"amount": "7.50", "currency": "USD"}})
    assert model["unitPrice"]["amount"] == "7.50"
    assert a.reads == 0  # nothing half-set → no read at all


def test_an_ordinary_write_costs_no_extra_request() -> None:
    a = _Adapter(_STORED)
    _complete(a, {"remark": "no money touched"})
    assert a.reads == 0


def test_an_unreadable_record_leaves_the_body_alone() -> None:
    """Better to let upstream answer than to guess at an amount."""
    a = _Adapter(None)
    model = _complete(a, {"unitPrice": {"currency": "USD"}})
    assert "amount" not in model["unitPrice"]


@pytest.mark.parametrize("path", ["prices.sale", "prices.purchase"])
def test_nested_pairs_are_found_too(path: str) -> None:
    """Product carries its pairs two levels down."""
    a = ProductAdapter()
    model = json.loads(json.dumps({"prices": {path.split(".")[1]: {"currency": "USD"}}}))
    pending = [
        p
        for p in a.money_pairs
        if isinstance((m := a._value_at(model, p)), dict)
        and m.get("currency") is not None
        and m.get("amount") is None
    ]
    assert pending == [path]
