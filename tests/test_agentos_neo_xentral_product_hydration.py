"""Product detail reads hydrate stock, bom and prices.sale from v1 sub-resources.

`describe` advertises all three, but the v3 product payload carries none of
them, so `map_read` could only ever emit empty ones (issue #23). On the mvp
tenant that meant `prices.sale: null` on every product while 61,256 sales
prices existed, and `bom.items: []` even where `hasBillOfMaterials` was true.

Hydration runs on single-record reads only — a 25-row list page would cost 75
extra round trips.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

from xentral_entity_cores.agentos_neo_xentral.emulated.product import (
    ProductAdapter,
    map_bom_items,
    map_stock,
    pick_sale_price,
)


class _Resp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Serves the three sub-resources; records which URLs were asked for."""

    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict | None = None) -> _Resp:
        self.calls.append(url)
        for suffix, resp in self.routes.items():
            if url.endswith(f"/{suffix}"):
                return resp
        return _Resp(404, {})


def _adapter(product: dict) -> ProductAdapter:
    adapter = ProductAdapter()

    async def _fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        return (200, {"data": product}) if handle else (200, {"data": [product]})

    adapter._get = _fake_get  # type: ignore[method-assign]
    return adapter


def _read(adapter: ProductAdapter, client: _FakeClient, *, handle: str | None = "prd_7") -> dict:
    resp = asyncio.run(
        adapter.request(
            method="GET",
            handle=handle,
            query=[],
            body=None,
            base_url="https://x.test",
            token="tok",
            accept_language=None,
            client=client,
        )
    )
    assert resp.status_code == 200
    return json.loads(resp.content)


# ---- pure mappers ---------------------------------------------------------


def test_stock_totals_map_to_the_model_block():
    got = map_stock({"data": {"totals": {"sellable": 75.0, "reserved": 20.0}}}, 100)
    assert got == {"available": 75, "reserved": 20, "incoming": None, "belowMinimum": True}


def test_below_minimum_is_none_when_either_side_is_unknown():
    assert map_stock({"data": {"totals": {"sellable": 5}}}, None)["belowMinimum"] is None
    assert map_stock({"data": {"totals": {"reserved": 1}}}, 10)["belowMinimum"] is None


def test_stock_without_totals_is_not_invented():
    assert map_stock({"data": {}}, 10) is None
    assert map_stock({}, 10) is None


def test_bom_items_carry_a_product_reference_and_quantity():
    got = map_bom_items(
        {
            "data": [
                {
                    "amount": "2.0000",
                    "product": {"id": "1102", "name": "Fabric", "number": "346"},
                    "type": "shopping part",
                    "reference": "REF-1",
                }
            ]
        }
    )
    assert got == [
        {
            "product": {
                "id": "prd_1102",
                "number": "346",
                "name": "Fabric",
                "href": "/v1/products/prd_1102",
            },
            "quantity": 2,
            "type": "shopping part",
            "reference": "REF-1",
        }
    ]


def test_bom_tolerates_junk_rows():
    assert map_bom_items({"data": ["nope", None, {}]}) == [
        {"product": None, "quantity": None, "type": None, "reference": None}
    ]
    assert map_bom_items({}) == []


def test_sale_price_picks_the_lowest_unscoped_tier():
    rows = {
        "data": [
            {"amount": "5", "price": {"amount": "5.00", "currency": "EUR"}},
            {"amount": "1", "price": {"amount": "9.00", "currency": "EUR"}},
        ]
    }
    assert pick_sale_price(rows, date(2026, 7, 27)) == {"amount": "9.00", "currency": "EUR"}


def test_sale_price_ignores_customer_and_group_scoped_rows():
    rows = {
        "data": [
            {"amount": "1", "customer": {"id": "3"}, "price": {"amount": "1.00"}},
            {"amount": "1", "customerGroup": "B2B", "price": {"amount": "2.00"}},
            {"amount": "9", "price": {"amount": "7.00", "currency": "EUR"}},
        ]
    }
    assert pick_sale_price(rows, date(2026, 7, 27)) == {"amount": "7.00", "currency": "EUR"}


def test_fully_expired_scale_yields_no_price():
    """mvp's prd_1: twenty tiers, all lapsed in 2023."""
    rows = {
        "data": [
            {"amount": str(q), "expiresAt": "2023-01-26", "price": {"amount": f"{q}.00"}}
            for q in range(1, 21)
        ]
    }
    assert pick_sale_price(rows, date(2026, 7, 27)) is None
    # ... and the same data read before expiry does resolve.
    assert pick_sale_price(rows, date(2022, 6, 1)) == {"amount": "1.00", "currency": "EUR"}


def test_not_yet_valid_row_is_skipped():
    rows = {"data": [{"amount": "1", "validFrom": "2027-01-01", "price": {"amount": "3.00"}}]}
    assert pick_sale_price(rows, date(2026, 7, 27)) is None


# ---- hydration on the read path -------------------------------------------


def test_detail_read_fills_all_three_sections():
    client = _FakeClient(
        {
            "stocks": _Resp(200, {"data": {"totals": {"sellable": 12, "reserved": 3}}}),
            "parts": _Resp(200, {"data": [{"amount": "2", "product": {"id": "9"}}]}),
            "salesPrices": _Resp(200, {"data": [{"amount": "1", "price": {"amount": "4.50"}}]}),
        }
    )
    body = _read(_adapter({"id": 7, "name": "Widget", "minimumStockLevel": 20}), client)
    rec = body["data"]
    assert rec["stock"] == {"available": 12, "reserved": 3, "incoming": None, "belowMinimum": True}
    assert rec["bom"]["items"][0]["product"]["id"] == "prd_9"
    assert rec["prices"]["sale"] == {"amount": "4.50", "currency": "EUR"}
    assert "extra" not in body
    assert sorted(u.rsplit("/", 1)[-1] for u in client.calls) == [
        "parts",
        "salesPrices",
        "stocks",
    ]


def test_list_reads_are_not_hydrated():
    client = _FakeClient({})
    _read(_adapter({"id": 7, "name": "Widget"}), client, handle=None)
    assert client.calls == []


def test_failed_subresource_is_reported_not_silently_empty():
    client = _FakeClient(
        {
            "stocks": _Resp(500, {}),
            "parts": _Resp(200, {"data": []}),
            "salesPrices": _Resp(200, {"data": []}),
        }
    )
    body = _read(_adapter({"id": 7, "name": "Widget"}), client)
    assert body["extra"]["unavailableSections"] == ["stock"]
    # The section keeps map_read's empty shape rather than a half-filled one.
    assert body["data"]["stock"]["available"] is None
    # A reachable-but-empty section is NOT reported as unavailable.
    assert body["data"]["bom"] == {"items": []}
    assert body["data"]["prices"]["sale"] is None


def test_hydration_failure_does_not_fail_the_product_read():
    client = _FakeClient({"stocks": _Resp(503, {}), "parts": _Resp(503, {})})
    body = _read(_adapter({"id": 7, "name": "Widget"}), client)
    assert body["data"]["name"] == "Widget"
    assert sorted(body["extra"]["unavailableSections"]) == ["bom", "prices.sale", "stock"]
