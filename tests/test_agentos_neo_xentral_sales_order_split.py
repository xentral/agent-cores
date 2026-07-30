"""SalesOrder splitOrder: move items/quantities into a new partial in one step.

`run op=splitOrder command={items:[{lineItem|product, quantity}]}` creates the
partial (v1 createPartialSalesOrder), adds the moved quantities to it, and reduces
the source order to the remainder (a fully-moved line is deleted). The two orders
together equal the original demand.
"""

from __future__ import annotations

import asyncio
import json

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


def _src_order() -> dict:
    def line(lid, order, prod, qty, price):
        return {
            "id": lid,
            "order": order,
            "product": {"id": prod},
            "quantity": qty,
            "unit": "piece",
            "price": {"net": {"amount": price, "currency": "EUR"}},
            "taxRate": "standard",
        }

    return {
        "id": 22917,
        "documentNumber": "221961",
        "financials": {"currency": "EUR"},
        "lineItems": [
            line("150983", 1, "61982", 3, "10"),
            line("150984", 2, "61983", 5, "20"),
            line("150985", 3, "61984", 4, "30"),
        ],
    }


def _adapter(calls: list):
    a = SalesOrderAdapter()

    async def fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        if "22918" in str(handle):
            return 200, {"data": {"id": 22918, "financials": {"currency": "EUR"}, "lineItems": []}}
        return 200, {"data": _src_order()}

    async def fake_create_partial(src_up, base_url, token, accept_language, client):  # noqa: ANN001, ANN202
        return "22918", 201, {}

    async def fake_li_call(method, url, token, al, client, payload=None):  # noqa: ANN001
        calls.append((method, url, payload))
        return (
            {"POST": 201, "PATCH": 200, "DELETE": 204}.get(method, 200),
            {"data": {"id": "999"}},
        )

    a._get = fake_get  # type: ignore[method-assign]
    a._create_partial = fake_create_partial  # type: ignore[method-assign]
    a._li_call = fake_li_call  # type: ignore[method-assign]
    return a


def _run(a, moves):
    body = json.dumps({"ids": ["so_22917"], "command": {"items": moves}}).encode()
    return asyncio.run(a._split_order("so_22917", body, "https://x", "t", None, None))


def test_split_moves_and_reduces():
    calls: list = []
    a = _adapter(calls)
    # move all of line Voll (3/3) and part of line Teil (2/5)
    resp = _run(a, [{"lineItem": "150983", "quantity": 3}, {"lineItem": "150984", "quantity": 2}])
    assert resp.status_code == 201

    posts = [c for c in calls if c[0] == "POST"]
    # both moved lines added to the PARTIAL (so_22918)
    assert len(posts) == 2
    assert all("/22918/lineItems" in c[1] for c in posts)
    assert posts[0][2]["quantity"] == 3 and posts[0][2]["product"] == {"id": "61982"}
    assert posts[1][2]["quantity"] == 2 and posts[1][2]["product"] == {"id": "61983"}

    # source (so_22917) reduced: Voll fully moved -> DELETE; Teil -> PATCH to 3
    dels = [c for c in calls if c[0] == "DELETE"]
    pats = [c for c in calls if c[0] == "PATCH"]
    assert any(c[1].endswith("/22917/lineItems/150983") for c in dels)
    assert any(c[1].endswith("/22917/lineItems/150984") and c[2] == {"quantity": 3} for c in pats)

    body = json.loads(resp.content)["data"]
    assert body["_split"]["partialId"] == "so_22918"


def test_move_by_product_reference():
    calls: list = []
    a = _adapter(calls)
    resp = _run(a, [{"product": "prd_61984", "quantity": 4}])
    assert resp.status_code == 201
    # TL Leer fully moved (4/4) -> DELETE the source line 150985
    assert any(c[0] == "DELETE" and c[1].endswith("/150985") for c in calls)


def test_move_exceeding_line_qty_is_rejected():
    calls: list = []
    a = _adapter(calls)
    resp = _run(a, [{"lineItem": "150984", "quantity": 99}])
    assert resp.status_code == 409
    assert calls == []  # nothing created or moved


def test_missing_items_is_422():
    calls: list = []
    a = _adapter(calls)
    resp = asyncio.run(
        a._split_order(
            "so_22917",
            json.dumps({"ids": ["so_22917"], "command": {}}).encode(),
            "https://x",
            "t",
            None,
            None,
        )
    )
    assert resp.status_code == 422
    assert calls == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
