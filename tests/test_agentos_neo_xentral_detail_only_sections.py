"""A list says which sections it did not load, and a half-set price reaches the composer.

Two findings from measuring `Product.prices.sale` on mvp, both of the same family:
a value that is absent for a reason the caller cannot see.

**The list is silent about what it left out.** `prices.sale` comes from
`/products/{id}/salesPrices`, which the adapter only calls on the SINGLE read —
one sub-request per row would be the alternative. So every product in a list
carries `prices.sale: null`, and an agent listing products concludes that none of
them has a sale price. Measured on mvp, same product, same core:

    LIST   -> prices.sale = null
    SINGLE -> prices.sale = {"amount": "9.90", "currency": "EUR"}

The single read already reports what it could not reach (`extra.unavailableSections`);
the list now reports what it never attempts, from the adapter's own declaration.

**The completed pair arrived too late to matter.** The currency-only completion ran
inside `_write_document`, but `Product._write` parses the request body itself first,
to decide whether to compose a salesPrices write. It saw the *incomplete* body,
found no amount, and skipped the composition — HTTP 200, price untouched. The
completion now runs at the entry point, before any parse. Measured after the fix:

    PATCH prices.sale = {"currency": "USD"}  ->  200, then {"amount": "9.90", "currency": "USD"}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter

# ---- the list names its unloaded sections --------------------------------


def test_product_declares_the_sections_it_only_fills_on_a_single_read() -> None:
    assert ProductAdapter.detail_only_sections == ("stock", "bom", "prices.sale", "properties")


def test_an_entity_without_sub_resources_declares_none() -> None:
    assert CustomerAdapter.detail_only_sections == ()


def _envelope(adapter: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return adapter._list_envelope(rows, {"data": rows, "meta": {"total": len(rows)}}, [])


def test_the_list_envelope_names_them() -> None:
    env = _envelope(ProductAdapter(), [{"id": "1", "name": "x"}])
    assert env["extra"]["unavailableSections"] == [
        "stock",
        "bom",
        "prices.sale",
        "properties",
    ]


def test_an_entity_with_nothing_to_declare_stays_quiet() -> None:
    """No empty key — the absence of the signal must not look like a claim."""
    env = _envelope(CustomerAdapter(), [{"id": "1"}])
    assert "unavailableSections" not in env["extra"]


# ---- the pair reaches the composer ---------------------------------------


class _Product(ProductAdapter):
    """Product with its reads stubbed: the stored record carries a sale price, so
    a currency-only write has something to complete from."""

    def __init__(self) -> None:
        super().__init__()
        self.sale_written: list[dict[str, Any]] = []

    async def request(self, **kw: Any):  # the pair completion reads through this
        if kw.get("method") == "GET" and kw.get("handle"):
            return self._json(
                200,
                {
                    "data": {
                        "id": "61994",
                        "prices": {"sale": {"amount": "9.90", "currency": "EUR"}},
                    }
                },
            )
        return await super().request(**kw)

    async def _write(self, method, handle, query, body, *a: Any):  # noqa: ANN001
        """Stand in for the real composition: record what `prices.sale` looked like
        by the time Product's own parse got hold of the body."""
        model = json.loads(body or b"{}")
        self.sale_written.append((model.get("prices") or {}).get("sale"))
        return self._json(200, {"data": {"id": "61994"}})


def _patch(adapter: _Product, model: dict[str, Any]) -> int:
    resp = asyncio.run(
        adapter.request(
            method="PATCH",
            handle="prd_61994",
            query=[],
            body=json.dumps(model).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )
    return resp.status_code


def test_a_currency_only_update_reaches_the_composer_complete() -> None:
    """The regression: Product's parse used to see `{"currency": "USD"}`, find no
    amount, and skip the sale-price write entirely."""
    a = _Product()
    assert _patch(a, {"prices": {"sale": {"currency": "USD"}}}) == 200
    assert a.sale_written == [{"currency": "USD", "amount": "9.90"}]


def test_a_complete_pair_is_passed_through_untouched() -> None:
    a = _Product()
    _patch(a, {"prices": {"sale": {"amount": "12.00", "currency": "EUR"}}})
    assert a.sale_written == [{"amount": "12.00", "currency": "EUR"}]


def test_a_create_is_never_completed_from_a_stored_record() -> None:
    """There is nothing stored yet — a POST must go out as the caller wrote it."""
    a = _Product()
    resp = asyncio.run(
        a.request(
            method="POST",
            handle=None,
            query=[],
            body=json.dumps({"prices": {"sale": {"currency": "USD"}}}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )
    assert resp.status_code == 200
    assert a.sale_written == [{"currency": "USD"}]


@pytest.mark.parametrize("body", [b"", b"not json", b"[]"])
def test_an_unparsable_body_is_forwarded_unchanged(body: bytes) -> None:
    """The completion must not turn a malformed body into a different error than
    upstream would give."""
    a = _Product()
    out = asyncio.run(
        a._complete_body_money_pairs("prd_1", body, "https://x.test", "t", None, None)
    )
    assert out is body
