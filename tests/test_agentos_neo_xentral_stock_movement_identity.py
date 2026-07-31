"""StockMovement write identity: the id comes from the upstream Location header.

The v1 storage-item endpoints answer with an EMPTY body and put the created
resource's URI in ``Location`` (same shape as the v2 product writes). Before this
the facade echoed ``id: null``, so a retried booking — normal agent behaviour on a
timeout — could neither be recognised nor referenced afterwards.

Pinned here: the id is taken from the header; a header pointing back at the
collection is NOT turned into a fabricated id; a transfer reports the arrival;
and a failed booking still compensates (the header plumbing must not change the
error path).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.stock_movement import StockMovementAdapter

_PRODUCT = {
    "id": "61985",
    "number": "E2E-0730-01",
    "name": "Trekkingzelt Aurora 2",
    "unit": "Stk",
}
_LOCATIONS = {
    "1": {"id": "1", "designation": "Lagerplatz1", "warehouse": {"id": "9", "name": "Hauptlager"}},
    "4": {"id": "4", "designation": "Lagerplatz2", "warehouse": {"id": "9", "name": "Hauptlager"}},
}


class _Upstream:
    def __init__(self, *, location_header: str | None = "auto", fail_call: int | None = None):
        self.location_header = location_header
        # 1-based index of the item call that fails; a later compensating call
        # must still succeed, so this is an index and not a method.
        self.fail_call = fail_call
        self.item_calls: list[tuple[str, str]] = []  # (method, path)
        self._next = 4711

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.startswith("/api/v3/products/"):
            return httpx.Response(200, json={"data": _PRODUCT})
        if path.startswith("/api/v1/products/"):  # stock hydration on the product read
            return httpx.Response(200, json={"data": {"id": "61985", "totals": {}}})
        if path == "/api/v1/storageLocations":
            wanted = request.url.params.get("filter[0][value]")
            row = _LOCATIONS.get(wanted)
            return httpx.Response(200, json={"data": [row] if row else []})
        if path.endswith("/items") and method in ("POST", "PATCH"):
            self.item_calls.append((method, path))
            if self.fail_call == len(self.item_calls):
                return httpx.Response(400, json={"title": "upstream says no"})
            headers = {}
            if self.location_header == "auto":
                headers["Location"] = f"https://unit.test{path}/{self._next}"
                self._next += 1
            elif self.location_header:
                headers["Location"] = self.location_header
            return httpx.Response(201, headers=headers)
        raise AssertionError(f"unexpected call: {method} {path}")


def _book(up: _Upstream, model: dict[str, Any]):
    a = StockMovementAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="POST",
                handle=None,
                query=[],
                body=json.dumps(model).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


_RECEIPT = {
    "type": "receipt",
    "product": {"id": "prd_61985"},
    "quantity": {"value": 5},
    "to": {"id": "loc_1"},
    "source": {"reason": "Manuelle Einlagerung"},
}


def test_receipt_id_comes_from_the_location_header():
    up = _Upstream()
    resp = _book(up, _RECEIPT)
    assert resp.status_code == 201
    data = json.loads(resp.content)["data"]
    assert data["id"] == "stm_4711"
    assert data["object"] == "stockMovement"
    assert up.item_calls == [("POST", "/api/v1/warehouses/9/storageLocations/1/items")]


def test_id_stays_null_when_upstream_sends_no_header():
    up = _Upstream(location_header=None)
    data = json.loads(_book(up, _RECEIPT).content)["data"]
    assert data["id"] is None  # not invented


def test_collection_header_is_not_turned_into_an_id():
    """A Location pointing at the collection must not yield ``stm_items``."""
    up = _Upstream(location_header="https://unit.test/api/v1/warehouses/9/storageLocations/1/items")
    data = json.loads(_book(up, _RECEIPT).content)["data"]
    assert data["id"] is None


def test_transfer_reports_the_arrival_booking():
    up = _Upstream()
    resp = _book(
        up,
        {
            "type": "transfer",
            "product": {"id": "prd_61985"},
            "quantity": {"value": 2},
            "from": {"id": "loc_1"},
            "to": {"id": "loc_4"},
        },
    )
    assert resp.status_code == 201
    data = json.loads(resp.content)["data"]
    assert [m for m, _ in up.item_calls] == ["PATCH", "POST"]
    assert data["id"] == "stm_4712"  # the second (arrival) call's Location


def test_failed_transfer_still_compensates():
    up = _Upstream(fail_call=2)  # the arrival POST fails, the compensation must not
    resp = _book(
        up,
        {
            "type": "transfer",
            "product": {"id": "prd_61985"},
            "quantity": {"value": 2},
            "from": {"id": "loc_1"},
            "to": {"id": "loc_4"},
        },
    )
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert body["compensation"] == "reverted"
    # retrieve, failed add, compensating add-back
    assert [m for m, _ in up.item_calls] == ["PATCH", "POST", "POST"]
