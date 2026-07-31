"""StorageLocation reads: warehouse scope and composed contents.

Two gaps that made the entity awkward to use rather than wrong:

* no `warehouse` filter — although the upstream list is warehouse-scoped BY
  NATURE (`/v1/warehouses/{id}/storageLocations`). Finding one warehouse's bins
  meant paging the whole tenant: 3 pages over 102 rows to reach 9 on mvp.
* `contents` always `[]` — the data is exactly what the StockLevel projection
  reports for that location.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.storage_location import (
    StorageLocationAdapter,
)

_FLAT = [
    {"id": "1", "designation": "Lagerplatz1", "warehouse": {"id": "9", "name": "Hauptlager"}},
    {"id": "7", "designation": "Amsterdam-01", "warehouse": {"id": "2", "name": "Amsterdam"}},
]


class _Upstream:
    def __init__(self, *, scoped_omits_warehouse: bool = True, levels_fail: bool = False):
        self.scoped_omits_warehouse = scoped_omits_warehouse
        self.levels_fail = levels_fail
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/api/v1/storageLocations":
            wanted = request.url.params.get("filter[0][value]")
            rows = [r for r in _FLAT if r["id"] == wanted] if wanted else _FLAT
            return httpx.Response(200, json={"data": rows, "extra": {"totalCount": len(rows)}})
        if path.startswith("/api/v1/warehouses/") and path.endswith("/storageLocations"):
            wh = path.split("/")[4]
            rows = [
                {"id": r["id"], "designation": r["designation"]}
                if self.scoped_omits_warehouse
                else r
                for r in _FLAT
                if r["warehouse"]["id"] == wh
            ]
            return httpx.Response(200, json={"data": rows, "extra": {"totalCount": len(rows)}})
        if path.startswith("/api/v2/warehouses/") and path.endswith("/items"):
            if self.levels_fail:
                return httpx.Response(500, json={"title": "down"})
            return httpx.Response(
                200,
                json={
                    "data": [{"productId": "61985", "sku": "E2E-1", "quantity": 7}],
                    "extra": {"totalCount": 1, "page": {"number": 1, "size": 50}},
                },
            )
        raise AssertionError(f"unexpected call: {request.method} {path}")


def _get(up: _Upstream, *, handle=None, query=None):
    a = StorageLocationAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="GET",
                handle=handle,
                query=list(query or []),
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


_WH_FILTER = [
    ("filter[0][key]", "warehouse"),
    ("filter[0][op]", "equals"),
    ("filter[0][value]", "wh_9"),
]


def test_warehouse_is_advertised_as_filterable():
    spec = StorageLocationAdapter().fields()["warehouse"]
    assert spec.get("filterable") is True


def test_warehouse_filter_reads_the_scoped_collection():
    up = _Upstream()
    resp = _get(up, query=_WH_FILTER)
    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert [r["id"] for r in body["data"]] == ["loc_1"]
    assert up.calls == ["/api/v1/warehouses/9/storageLocations"]  # one call, not three pages


def test_the_scoped_rows_keep_their_warehouse_even_though_the_path_implies_it():
    up = _Upstream(scoped_omits_warehouse=True)
    body = json.loads(_get(up, query=_WH_FILTER).content)
    assert body["data"][0]["warehouse"]["id"] == "wh_9"


def test_an_unknown_warehouse_reports_the_upstream_status():
    up = _Upstream()
    up.handler  # noqa: B018
    resp = _get(
        up,
        query=[
            ("filter[0][key]", "warehouse"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "wh_404"),
        ],
    )
    # the fake answers 200 with no rows for an unknown warehouse
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"] == []


def test_single_read_goes_through_the_id_filter_not_a_show_route():
    """v1 has no GET /storageLocations/{id} — it answers 404 (verified on mvp),
    so `read` was declared and could not work."""
    up = _Upstream()
    resp = _get(up, handle="loc_1")
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"]["id"] == "loc_1"
    assert "/api/v1/storageLocations/1" not in up.calls
    assert up.calls[0] == "/api/v1/storageLocations"


def test_unknown_location_is_404():
    up = _Upstream()
    assert _get(up, handle="loc_999").status_code == 404


def test_single_read_composes_contents():
    up = _Upstream()
    resp = _get(up, handle="loc_1")
    assert resp.status_code == 200
    contents = json.loads(resp.content)["data"]["contents"]
    assert len(contents) == 1
    assert contents[0]["product"]["id"] == "prd_61985"
    assert contents[0]["quantity"]["value"] == 7.0


def test_list_does_not_compose_contents():
    """One extra call per row is the wrong trade on a list; the field description
    points at StockLevel instead."""
    up = _Upstream()
    resp = _get(up)
    body = json.loads(resp.content)
    assert all(r["contents"] == [] for r in body["data"])
    assert not any(c.startswith("/api/v2/") for c in up.calls)


def test_unreadable_levels_leave_contents_empty_rather_than_claim_an_empty_bin():
    up = _Upstream(levels_fail=True)
    resp = _get(up, handle="loc_1")
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"]["contents"] == []
