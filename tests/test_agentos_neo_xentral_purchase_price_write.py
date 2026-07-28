"""PurchasePrice write: supplier purchase prices incl. scale tiers, on v2.

Each record is ONE supplier price row (one quantity tier), so a real purchase scale
price (Einkaufs-Staffelpreis) is several entries differing only in ``minQuantity``.
These pin:
  * map_write → the v2 body (``fromQuantity`` tier, ``internalComment`` remark),
    product sent only on create, price a STRING on create / NUMBER on update;
  * create → POST v2 (id from the Location header) then read-back via list-by-id;
  * update → PATCH v2 /{id}; delete → DELETE v1 /{id};
  * a multi-tier purchase scale price is writable as several entries.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_price import PurchasePriceAdapter

_ENTRY: dict[str, Any] = {
    "product": {"id": "prd_5"},
    "supplier": {"id": "sup_7"},
    "isStandardSupplier": True,
    "supplierItemNumber": "4992042-20",
    "minQuantity": 10,
    "packageAmount": 5,
    "unitPrice": {"amount": "9.50", "currency": "EUR"},
    "validFrom": "2026-08-01",
    "validUntil": "2026-12-31",
    "remark": "bulk tier",
    # read-emitted keys that must be ignored, not rejected
    "object": "purchasePrice",
    "id": "pp_1",
}


def test_operations_include_write():
    a = PurchasePriceAdapter()
    assert set(a.manifest.operations) == {"list", "read", "create", "update", "delete"}


def test_map_write_create_v2_body():
    body, rejected = PurchasePriceAdapter().map_write(_ENTRY, creating=True)
    assert rejected == set()
    assert body["product"] == {"id": "5"}
    assert body["supplier"] == {"id": "7"}
    assert body["isStandardSupplier"] is True
    assert body["supplierItemNumber"] == "4992042-20"
    assert body["fromQuantity"] == 10  # the purchase scale tier
    assert body["packageAmount"] == 5
    assert body["price"] == {"amount": "9.50", "currency": "EUR"}  # string on create
    assert body["validFrom"] == "2026-08-01"
    assert body["expiresAt"] == "2026-12-31"
    assert body["internalComment"] == "bulk tier"


def test_map_write_update_drops_product_and_numbers_price():
    body, rejected = PurchasePriceAdapter().map_write(
        {"product": {"id": "prd_5"}, "unitPrice": {"amount": "8.00", "currency": "EUR"}},
        creating=False,
    )
    assert rejected == set()
    assert "product" not in body  # v2 PATCH has no product
    assert body["price"] == {"amount": 8.0, "currency": "EUR"}  # number on update


def test_map_write_defaults_base_tier_on_create():
    body, _ = PurchasePriceAdapter().map_write(
        {"product": {"id": "prd_5"}, "unitPrice": {"amount": "5.00"}}, creating=True
    )
    assert body["fromQuantity"] == 1


def test_map_write_rejects_unknown_key():
    _, rejected = PurchasePriceAdapter().map_write({"bogus": 1}, creating=True)
    assert rejected == {"bogus"}


class _Upstream:
    """Stateful fake Xentral v2 purchasePrices (+ v1 delete)."""

    def __init__(self):
        self.post_paths: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self._next = 500
        self.rows: dict[str, dict[str, Any]] = {}

    def _store(self, body: dict[str, Any]) -> str:
        rid = str(self._next)
        self._next += 1
        self.rows[rid] = {
            "id": rid,
            "product": body.get("product"),
            "supplier": body.get("supplier"),
            "isStandardSupplier": body.get("isStandardSupplier"),
            "supplierItemNumber": body.get("supplierItemNumber"),
            "fromQuantity": body.get("fromQuantity"),
            "packageAmount": body.get("packageAmount"),
            "price": {
                "amount": str(body["price"]["amount"]),
                "currency": body["price"]["currency"],
            },
            "validFrom": body.get("validFrom"),
            "expiresAt": body.get("expiresAt"),
            "internalComment": body.get("internalComment"),
        }
        return rid

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v2/purchasePrices" and method == "POST":
            self.post_paths.append(path)
            body = json.loads(request.content)
            self.posts.append(body)
            rid = self._store(body)
            return httpx.Response(
                201, headers={"Location": f"https://unit.test/api/v2/purchasePrices/{rid}"}
            )
        if path == "/api/v2/purchasePrices" and method == "GET":
            wanted = request.url.params.get("filter[0][value]")
            rows = [self.rows[wanted]] if wanted in self.rows else []
            return httpx.Response(
                200,
                json={
                    "data": rows,
                    "extra": {"totalCount": len(rows), "page": {"number": 1, "size": 10}},
                },
            )
        if path.startswith("/api/v2/purchasePrices/") and method == "PATCH":
            rid = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            self.patches.append((path, body))
            row = self.rows[rid]
            if "price" in body:
                row["price"] = {
                    "amount": str(body["price"]["amount"]),
                    "currency": body["price"]["currency"],
                }
            for k in ("fromQuantity", "validFrom", "expiresAt", "internalComment"):
                if k in body:
                    row[k] = body[k]
            return httpx.Response(204)
        if path.startswith("/api/v1/purchasePrices/") and method == "DELETE":
            self.deletes.append(path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {method} {path}")


def _call(up: _Upstream, *, method: str, handle: str | None, model: dict[str, Any] | None):
    a = PurchasePriceAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method=method,
                handle=handle,
                query=[],
                body=json.dumps(model).encode() if model is not None else None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def test_create_posts_v2_then_reads_back():
    up = _Upstream()
    resp = _call(up, method="POST", handle=None, model=_ENTRY)
    assert resp.status_code == 201
    assert up.post_paths == ["/api/v2/purchasePrices"]
    assert up.posts[0]["fromQuantity"] == 10
    data = json.loads(resp.content)["data"]
    assert data["id"] == "pp_500"  # id came from the Location header
    assert data["object"] == "purchasePrice"
    assert data["minQuantity"] == 10
    assert data["unitPrice"]["amount"] == "9.50"
    assert data["supplier"]["id"] == "sup_7"


def test_get_reads_single_via_list_by_id():
    up = _Upstream()
    up.rows["500"] = {
        "id": "500",
        "product": {"id": "5"},
        "supplier": {"id": "7"},
        "isStandardSupplier": True,
        "supplierItemNumber": "X",
        "fromQuantity": 10,
        "packageAmount": 1,
        "price": {"amount": "9.50", "currency": "EUR"},
        "validFrom": None,
        "expiresAt": None,
        "internalComment": None,
    }
    resp = _call(up, method="GET", handle="pp_500", model=None)
    assert resp.status_code == 200
    data = json.loads(resp.content)["data"]
    assert data["id"] == "pp_500"
    assert data["minQuantity"] == 10


def test_update_patches_v2():
    up = _Upstream()
    up.rows["500"] = {
        "id": "500",
        "product": {"id": "5"},
        "supplier": {"id": "7"},
        "isStandardSupplier": True,
        "supplierItemNumber": "X",
        "fromQuantity": 10,
        "packageAmount": 1,
        "price": {"amount": "9.50", "currency": "EUR"},
        "validFrom": None,
        "expiresAt": None,
        "internalComment": None,
    }
    resp = _call(
        up,
        method="PATCH",
        handle="pp_500",
        model={"unitPrice": {"amount": "8.00", "currency": "EUR"}, "validFrom": "2026-09-01"},
    )
    assert resp.status_code == 200
    assert up.patches[0][0] == "/api/v2/purchasePrices/500"
    assert up.patches[0][1] == {
        "price": {"amount": 8.0, "currency": "EUR"},
        "validFrom": "2026-09-01",
    }
    data = json.loads(resp.content)["data"]
    assert data["unitPrice"]["amount"] == "8.00"


def test_delete_uses_v1():
    up = _Upstream()
    resp = _call(up, method="DELETE", handle="pp_500", model=None)
    assert resp.status_code == 204
    assert up.deletes == ["/api/v1/purchasePrices/500"]


def test_multi_tier_purchase_scale_price_is_writable():
    up = _Upstream()
    tiers = [(1, "12.00"), (10, "10.50"), (100, "9.00")]
    ids = []
    for qty, price in tiers:
        resp = _call(
            up,
            method="POST",
            handle=None,
            model={
                "product": {"id": "prd_5"},
                "supplier": {"id": "sup_7"},
                "minQuantity": qty,
                "unitPrice": {"amount": price, "currency": "EUR"},
            },
        )
        assert resp.status_code == 201
        ids.append(json.loads(resp.content)["data"]["id"])
    assert [b["fromQuantity"] for b in up.posts] == [1, 10, 100]
    assert len(set(ids)) == 3
