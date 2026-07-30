"""SalesOrder per-line availability (computed on the single `get`).

Each line's product is read once (v2: isStockItem + stockCount) and the line gains
an `availability` block: stock items deliver min(ordered, on-hand); non-stock items
carry no stock constraint so the full ordered quantity is deliverable.
"""

from __future__ import annotations

import asyncio
import json

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


def _avail(product_data: dict, ordered):
    a = SalesOrderAdapter()

    async def fake_li_call(method, url, token, al, client, payload=None):  # noqa: ANN001
        return 200, {"data": product_data}

    a._li_call = fake_li_call  # type: ignore[assignment]
    return asyncio.run(a._product_availability("61981", ordered, "https://x", "t", None, None))


def test_stock_item_partially_deliverable():
    assert _avail({"isStockItem": True, "stockCount": 2}, 5) == {
        "stockManaged": True,
        "onHand": 2,
        "deliverable": 2,
    }


def test_stock_item_fully_deliverable():
    assert _avail({"isStockItem": True, "stockCount": 10}, 3) == {
        "stockManaged": True,
        "onHand": 10,
        "deliverable": 3,
    }


def test_stock_item_nothing_on_hand():
    assert _avail({"isStockItem": True, "stockCount": 0}, 4)["deliverable"] == 0


def test_non_stock_is_unlimited():
    assert _avail({"isStockItem": False, "stockCount": 0}, 4) == {
        "stockManaged": False,
        "onHand": None,
        "deliverable": 4,
    }


def test_product_read_failure_leaves_line_bare():
    a = SalesOrderAdapter()

    async def fail(method, url, token, al, client, payload=None):  # noqa: ANN001
        return 500, {}

    a._li_call = fail  # type: ignore[assignment]
    assert asyncio.run(a._product_availability("1", 5, "https://x", "t", None, None)) is None


def test_hydrate_attaches_availability_per_line():
    a = SalesOrderAdapter()

    async def fake_pa(up, ordered, base_url, token, al, client):  # noqa: ANN001
        return {"stockManaged": True, "onHand": 2, "deliverable": min(ordered, 2)}

    a._product_availability = fake_pa  # type: ignore[assignment]
    resp = a._json(
        200,
        {
            "data": {
                "items": [
                    {"id": "1", "product": {"id": "prd_61981"}, "quantity": {"value": 5}},
                    {"id": "2", "product": {"id": "prd_9"}, "quantity": {"value": 1}},
                ]
            }
        },
    )
    out = asyncio.run(a._hydrate_availability(resp, "https://x", "t", None, None))
    items = json.loads(out.content)["data"]["items"]
    assert items[0]["availability"]["deliverable"] == 2
    assert items[1]["availability"]["deliverable"] == 1


def test_hydrate_noop_on_error_response():
    a = SalesOrderAdapter()
    resp = a._json(409, {"title": "nope"})
    out = asyncio.run(a._hydrate_availability(resp, "https://x", "t", None, None))
    assert out.status_code == 409


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
