"""SalesOrder line items are editable on UPDATE via the v3 lineItems sub-resource.

The order PATCH cannot carry line items (v3 has no lineItems slot on the document),
but v3 grew a full lineItems sub-resource (POST/PATCH/DELETE
/api/v3/salesOrders/{id}/lineItems). So `update` with `items` reconciles as a
collection replace: an item with an existing id is PATCHed, one without an id is
POSTed, and an existing item omitted from the list is DELETEd.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records the v3 lineItems sub-resource calls the reconcile makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def request(self, method: str, url: str, json: Any = None, headers: dict | None = None):  # noqa: A002
        self.calls.append((method, url, json))
        if method == "POST":
            return _Resp(201, {"data": {"id": "150999"}})
        if method == "PATCH":
            return _Resp(200, {"data": {"id": url.rsplit("/", 1)[-1]}})
        if method == "DELETE":
            return _Resp(204, {})
        return _Resp(200, {})


def _order(li_ids: list[int]) -> dict:
    return {
        "id": 22911,
        "documentNumber": "AB-1",
        "financials": {"currency": "EUR"},
        "lineItems": [
            {"id": i, "order": n + 1, "product": {"id": "1"}, "quantity": 1, "unit": "piece"}
            for n, i in enumerate(li_ids)
        ],
    }


def _adapter(order: dict) -> SalesOrderAdapter:
    a = SalesOrderAdapter()

    async def _fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        return (200, {"data": order})

    a._get = _fake_get  # type: ignore[method-assign]
    return a


def _suffix(url: str) -> str:
    return url.split("/lineItems", 1)[1]  # "" for the collection, "/<id>" for an item


def _run(adapter: SalesOrderAdapter, client: _FakeClient, items: list[dict], *, dry: bool = False):
    query = [("dryRun", "true")] if dry else []
    return asyncio.run(
        adapter.request(
            method="PATCH",
            handle="so_22911",
            query=query,
            body=json.dumps({"items": items}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=client,
        )
    )


def test_reconcile_deletes_patches_and_adds():
    client = _FakeClient()
    adapter = _adapter(_order([150975, 150976, 150977]))
    resp = _run(
        adapter,
        client,
        [
            # keep #1 but change the quantity
            {
                "id": "150975",
                "product": {"id": "prd_61976"},
                "quantity": {"value": 10, "unit": "piece"},
                "unitPrice": {"amount": 11, "currency": "EUR"},
            },
            # keep #3 unchanged (id only)
            {"id": "150977"},
            # add a new article ×4
            {
                "product": {"id": "prd_61974"},
                "quantity": {"value": 4, "unit": "piece"},
                "unitPrice": {"amount": 44, "currency": "EUR"},
            },
        ],
    )
    assert resp.status_code == 200
    ops = [(m, _suffix(u)) for m, u, _ in client.calls]
    assert ("DELETE", "/150976") in ops  # the omitted line removed
    assert ("PATCH", "/150975") in ops  # the kept-with-change line patched
    assert ("POST", "") in ops  # the new line added to the collection
    # the unchanged line is neither patched nor deleted
    assert ("DELETE", "/150977") not in ops
    assert ("PATCH", "/150977") not in ops
    # the PATCH body carries the new quantity, not the product (fixed on a line)
    patch_body = next(b for m, u, b in client.calls if m == "PATCH" and u.endswith("/150975"))
    assert patch_body.get("quantity") == 10
    assert "product" not in patch_body


def test_dry_run_does_not_touch_line_items():
    client = _FakeClient()
    adapter = _adapter(_order([150975]))
    _run(adapter, client, [{"product": {"id": "prd_61974"}, "quantity": {"value": 1}}], dry=True)
    assert client.calls == []  # bulk_validate must not write


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
