"""StockLevel: the read-back path for stock, composed from the v1/v2 fan-in.

There is no unified upstream stock collection, so this projection is anchored:

  * ``filter[product]``         -> GET /v1/products/{id}/storageLocations
  * ``filter[storageLocation]`` -> GET /v2/warehouses/{wh}/storageLocations/{loc}/items

These pin the anchor contract (an unanchored list must be refused, not fanned out
over the whole tenant), both anchors' projections, the client-side narrowing by
warehouse, upstream paging, and the composite read id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.stock_level import StockLevelAdapter

_LOCATIONS = {
    "1": {"id": "1", "designation": "Lagerplatz1", "warehouse": {"id": "9", "name": "Hauptlager"}},
    "4": {"id": "4", "designation": "Lagerplatz2", "warehouse": {"id": "9", "name": "Hauptlager"}},
    "7": {"id": "7", "designation": "Amsterdam-01", "warehouse": {"id": "2", "name": "Amsterdam"}},
}


class _Upstream:
    """Fake v1 product-storageLocations + v1 storageLocations + v2 items."""

    def __init__(self, *, product_rows: list[dict[str, Any]] | None = None):
        self.calls: list[str] = []
        self.product_rows = (
            product_rows
            if product_rows is not None
            else [
                {
                    "id": "100",
                    "amount": "15.00",
                    "product": {"id": "61985"},
                    "storageLocation": {
                        "id": "1",
                        "name": "Lagerplatz1",
                        "warehouse": {"id": "9", "name": "Hauptlager"},
                    },
                },
                {
                    "id": "101",
                    "amount": "3.00",
                    "product": {"id": "61985"},
                    "storageLocation": {
                        "id": "7",
                        "name": "Amsterdam-01",
                        "warehouse": {"id": "2", "name": "Amsterdam"},
                    },
                },
            ]
        )
        self.items = [
            {"productId": "61985", "sku": "E2E-0730-01", "quantity": 15},
            {"productId": "500", "sku": "ABC-1", "quantity": 2},
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        params = request.url.params
        if path.startswith("/api/v1/products/") and path.endswith("/storageLocations"):
            page = int(params.get("page[number]") or "1")
            size = int(params.get("page[size]") or "50")
            window = self.product_rows[(page - 1) * size : (page - 1) * size + size]
            return httpx.Response(
                200, json={"data": window, "extra": {"page": {"number": page, "size": size}}}
            )
        if path == "/api/v1/storageLocations":
            wanted = params.get("filter[0][value]")
            row = _LOCATIONS.get(wanted)
            return httpx.Response(200, json={"data": [row] if row else []})
        if path.startswith("/api/v2/warehouses/") and path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "data": self.items,
                    "extra": {"totalCount": len(self.items), "page": {"number": 1, "size": 50}},
                },
            )
        raise AssertionError(f"unexpected call: {request.method} {path}")


def _call(
    up: _Upstream,
    *,
    handle: str | None = None,
    filters: dict[str, str] | None = None,
    query: list[tuple[str, str]] | None = None,
    method: str = "GET",
):
    a = StockLevelAdapter()
    q: list[tuple[str, str]] = list(query or [])
    for i, (key, value) in enumerate((filters or {}).items()):
        q += [
            (f"filter[{i}][key]", key),
            (f"filter[{i}][op]", "equals"),
            (f"filter[{i}][value]", value),
        ]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method=method,
                handle=handle,
                query=q,
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def test_unanchored_list_is_refused_without_calling_upstream():
    up = _Upstream()
    resp = _call(up)
    assert resp.status_code == 422
    body = json.loads(resp.content)
    assert body["anchors"] == ["product", "storageLocation"]
    assert up.calls == []  # never fan out over the tenant


def test_warehouse_alone_cannot_anchor():
    up = _Upstream()
    resp = _call(up, filters={"warehouse": "wh_9"})
    assert resp.status_code == 422
    assert up.calls == []


def test_product_anchor_projects_every_location():
    up = _Upstream()
    resp = _call(up, filters={"product": "prd_61985"})
    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert body["meta"]["total"] == 2
    first = body["data"][0]
    assert first["id"] == "slv_61985_1"
    assert first["object"] == "stockLevel"
    assert first["product"]["id"] == "prd_61985"
    assert first["storageLocation"]["id"] == "loc_1"
    assert first["storageLocation"]["name"] == "Lagerplatz1"
    assert first["warehouse"]["id"] == "wh_9"
    assert first["quantity"]["value"] == 15.0
    # honest blanks — no per-location reservation upstream
    assert first["reserved"]["value"] is None
    assert first["available"]["value"] is None
    assert first["batch"] is None
    assert up.calls == ["/api/v1/products/61985/storageLocations"]


def test_warehouse_narrows_a_product_anchor():
    up = _Upstream()
    resp = _call(up, filters={"product": "prd_61985", "warehouse": "wh_2"})
    body = json.loads(resp.content)
    assert [r["storageLocation"]["id"] for r in body["data"]] == ["loc_7"]
    assert body["meta"]["total"] == 1


def test_storage_location_narrows_a_product_anchor():
    up = _Upstream()
    resp = _call(up, filters={"product": "prd_61985", "storageLocation": "loc_1"})
    body = json.loads(resp.content)
    assert [r["id"] for r in body["data"]] == ["slv_61985_1"]


def test_location_anchor_projects_every_product():
    up = _Upstream()
    resp = _call(up, filters={"storageLocation": "loc_1"})
    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert body["meta"]["total"] == 2
    row = body["data"][0]
    assert row["id"] == "slv_61985_1"
    assert row["product"]["id"] == "prd_61985"
    assert row["product"]["number"] == "E2E-0730-01"
    assert row["storageLocation"]["name"] == "Lagerplatz1"
    assert row["warehouse"]["id"] == "wh_9"
    assert row["quantity"]["value"] == 15.0
    # the warehouse of the location had to be resolved first
    assert up.calls == [
        "/api/v1/storageLocations",
        "/api/v2/warehouses/9/storageLocations/1/items",
    ]


def test_unknown_location_anchor_is_404_not_empty_success():
    up = _Upstream()
    resp = _call(up, filters={"storageLocation": "loc_999"})
    assert resp.status_code == 404


def test_product_anchor_pages_upstream_until_short_page():
    rows = [
        {
            "id": str(i),
            "amount": "1.00",
            "product": {"id": "7"},
            "storageLocation": {
                "id": str(i),
                "name": f"L{i}",
                "warehouse": {"id": "9", "name": "Hauptlager"},
            },
        }
        for i in range(1, 61)  # two upstream pages at size 50
    ]
    up = _Upstream(product_rows=rows)
    resp = _call(up, filters={"product": "prd_7"}, query=[("page[size]", "100")])
    body = json.loads(resp.content)
    assert body["meta"]["total"] == 60
    assert len(body["data"]) == 60
    assert up.calls.count("/api/v1/products/7/storageLocations") == 2


def test_paging_slices_the_composed_rows():
    up = _Upstream()
    resp = _call(
        up,
        filters={"product": "prd_61985"},
        query=[("page[number]", "2"), ("page[size]", "1")],
    )
    body = json.loads(resp.content)
    assert [r["id"] for r in body["data"]] == ["slv_61985_7"]
    assert body["meta"]["total"] == 2


def test_read_one_by_composite_id():
    up = _Upstream()
    resp = _call(up, handle="slv_61985_7")
    assert resp.status_code == 200
    data = json.loads(resp.content)["data"]
    assert data["id"] == "slv_61985_7"
    assert data["quantity"]["value"] == 3.0


def test_read_one_unknown_location_is_404():
    up = _Upstream()
    resp = _call(up, handle="slv_61985_999")
    assert resp.status_code == 404


def test_read_one_malformed_id_is_422():
    up = _Upstream()
    resp = _call(up, handle="slv_61985")
    assert resp.status_code == 422
    assert up.calls == []


def test_batch_filter_is_refused_as_a_grain():
    up = _Upstream()
    resp = _call(up, filters={"product": "prd_61985", "batch": "b_1"})
    assert resp.status_code == 422
    assert "grain" in json.loads(resp.content)["title"]


def test_write_is_rejected_by_the_operations_gate():
    up = _Upstream()
    a = StockLevelAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="POST",
                handle=None,
                query=[],
                body=b"{}",
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    resp = asyncio.run(go())
    assert resp.status_code == 405
    assert up.calls == []
