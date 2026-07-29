"""PriceList write: sales-price rows composed on the salesPrices resource.

Each PriceList record is ONE sales-price row (one quantity tier), so a real scale
price (Staffelpreis) is several entries differing only in ``minQuantity``. These pin:
  * map_write → the canonical v3 body (``quantity`` tier name), product + scope sent
    only on create, unknown keys rejected;
  * create prefers v3 salesPrices and falls back to v1 (Location-header id) when v3
    is unavailable; the v1 body renames the tier to ``amount``;
  * update goes to v1 PATCH /{id} (v3 has no single-record update);
  * delete prefers v3 then v1;
  * a multi-tier scale price is writable as several entries.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.price_list import PriceListAdapter

_ENTRY: dict[str, Any] = {
    "product": {"id": "prd_12"},
    "scope": {"customer": {"id": "cus_7"}},
    "minQuantity": 10,
    "unitPrice": {"amount": "9.50", "currency": "EUR"},
    "validFrom": "2026-08-01",
    "validUntil": "2026-12-31",
    "remark": "B2B tier",
    # read-emitted / computed keys that must be ignored, not rejected
    "object": "priceListEntry",
    "id": "ple_1",
    "currency": "EUR",
    "entries": None,
}


def test_operations_include_write():
    a = PriceListAdapter()
    assert set(a.manifest.operations) == {"list", "read", "create", "update", "delete"}


def test_map_write_create_canonical_v3_body():
    body, rejected = PriceListAdapter().map_write(_ENTRY, creating=True)
    assert rejected == set()
    assert body["product"] == {"id": "12"}  # speaking prefix stripped
    assert body["customer"] == {"id": "7"}
    assert body["quantity"] == 10  # canonical v3 tier name
    assert body["price"] == {"amount": "9.50", "currency": "EUR"}
    assert body["validFrom"] == "2026-08-01"
    assert body["expiresAt"] == "2026-12-31"  # validUntil → expiresAt
    assert body["remark"] == "B2B tier"
    assert "amount" not in body  # v1 name never leaks into the canonical body


def test_map_write_update_drops_identity_fields():
    # product + scope are the row's identity → not sent on update; the price is.
    body, rejected = PriceListAdapter().map_write(
        {
            "product": {"id": "prd_12"},
            "scope": {"customer": {"id": "cus_7"}},
            "unitPrice": {"amount": "8.00", "currency": "EUR"},
        },
        creating=False,
    )
    assert rejected == set()
    assert "product" not in body and "customer" not in body
    assert body["price"] == {"amount": "8.00", "currency": "EUR"}


def test_map_write_defaults_base_tier_on_create():
    body, _ = PriceListAdapter().map_write(
        {"product": {"id": "prd_12"}, "unitPrice": {"amount": "5.00"}}, creating=True
    )
    assert body["quantity"] == 1  # no minQuantity → base tier


def test_map_write_rejects_unknown_key():
    _, rejected = PriceListAdapter().map_write({"bogus": 1}, creating=True)
    assert rejected == {"bogus"}


class _Upstream:
    """Stateful fake Xentral salesPrices (v3 primary + v1 fallback)."""

    def __init__(self, *, v3_available: bool = True, groups_status: int = 200):
        self.v3_available = v3_available
        self.post_paths: list[str] = []  # every POST attempt (incl. a v3 that 404s)
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self._next = 500
        self.rows: dict[str, dict[str, Any]] = {}
        # GET /api/v1/groups (the customer-group guard reads this). Ids 11/12 exist.
        self.groups_status = groups_status
        self.groups = [
            {"id": "11", "name": "Preisgruppe A", "type": "priceGroup"},
            {"id": "12", "name": "Preisgruppe B", "type": "priceGroup"},
        ]
        self.group_reads = 0

    def _store(self, body: dict[str, Any], *, tier_key: str) -> str:
        rid = str(self._next)
        self._next += 1
        self.rows[rid] = {
            "id": rid,
            "product": body.get("product"),
            "customer": body.get("customer"),
            "customerGroup": body.get("customerGroup"),
            "amount": body.get(tier_key),
            "price": {
                "amount": body["price"]["amount"],
                "currency": body["price"]["currency"],
            },
            "validFrom": body.get("validFrom"),
            "expiresAt": body.get("expiresAt"),
            "remark": body.get("remark"),
        }
        return rid

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v1/groups" and method == "GET":
            self.group_reads += 1
            if self.groups_status >= 400:
                return httpx.Response(self.groups_status, json={"title": "boom"})
            return httpx.Response(200, json={"data": self.groups})
        if path == "/api/v3/salesPrices" and method == "POST":
            self.post_paths.append(path)
            if not self.v3_available:
                return httpx.Response(404, json={"title": "not found"})
            body = json.loads(request.content)
            self.posts.append((path, body))
            rid = self._store(body, tier_key="quantity")
            return httpx.Response(201, json={"data": {"id": rid}})
        if path == "/api/v1/salesPrices" and method == "POST":
            self.post_paths.append(path)
            body = json.loads(request.content)
            self.posts.append((path, body))
            rid = self._store(body, tier_key="amount")
            # v1 answers empty body + Location header
            return httpx.Response(
                201, headers={"Location": f"https://unit.test/api/v1/salesPrices/{rid}"}
            )
        if path.startswith("/api/v1/salesPrices/") and method == "GET":
            rid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"data": self.rows[rid]})
        if path.startswith("/api/v1/salesPrices/") and method == "PATCH":
            rid = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            self.patches.append((path, body))
            row = self.rows[rid]
            if "price" in body:
                row["price"] = {
                    "amount": body["price"]["amount"],
                    "currency": body["price"]["currency"],
                }
            for k in ("amount", "validFrom", "expiresAt", "remark"):
                if k in body:
                    row[k] = body[k]
            return httpx.Response(204)
        if path.startswith("/api/v3/salesPrices/") and method == "DELETE":
            if not self.v3_available:
                return httpx.Response(404, json={"title": "not found"})
            self.deletes.append(path)
            return httpx.Response(204)
        if path.startswith("/api/v1/salesPrices/") and method == "DELETE":
            self.deletes.append(path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {method} {path}")


def _call(up: _Upstream, *, method: str, handle: str | None, model: dict[str, Any] | None):
    a = PriceListAdapter()

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


def test_create_prefers_v3_then_reads_back():
    up = _Upstream()
    resp = _call(up, method="POST", handle=None, model=_ENTRY)
    assert resp.status_code == 201
    assert up.posts[0][0] == "/api/v3/salesPrices"
    assert up.posts[0][1]["quantity"] == 10
    data = json.loads(resp.content)["data"]
    assert data["id"] == "ple_500"
    assert data["minQuantity"] == 10
    assert data["unitPrice"]["amount"] == "9.50"
    assert data["object"] == "priceListEntry"


def test_create_falls_back_to_v1_when_v3_absent():
    up = _Upstream(v3_available=False)
    resp = _call(up, method="POST", handle=None, model=_ENTRY)
    assert resp.status_code == 201
    # v3 404 → v1 create; the v1 body renames the tier to `amount`
    assert up.post_paths == ["/api/v3/salesPrices", "/api/v1/salesPrices"]
    v1_body = up.posts[0][1]
    assert v1_body["amount"] == 10 and "quantity" not in v1_body
    data = json.loads(resp.content)["data"]  # id came from the Location header
    assert data["id"] == "ple_500"
    assert data["minQuantity"] == 10


def test_update_goes_to_v1_patch():
    up = _Upstream()
    # seed a row to update
    up.rows["500"] = {
        "id": "500",
        "product": {"id": "12"},
        "customer": {"id": "7"},
        "customerGroup": None,
        "amount": 10,
        "price": {"amount": "9.50", "currency": "EUR"},
        "validFrom": None,
        "expiresAt": None,
        "remark": None,
    }
    resp = _call(
        up,
        method="PATCH",
        handle="ple_500",
        model={"unitPrice": {"amount": "8.00", "currency": "EUR"}, "validFrom": "2026-09-01"},
    )
    assert resp.status_code == 200
    assert up.patches[0][0] == "/api/v1/salesPrices/500"
    assert up.patches[0][1] == {
        "price": {"amount": "8.00", "currency": "EUR"},
        "validFrom": "2026-09-01",
    }
    data = json.loads(resp.content)["data"]
    assert data["unitPrice"]["amount"] == "8.00"


def test_delete_prefers_v3():
    up = _Upstream()
    resp = _call(up, method="DELETE", handle="ple_500", model=None)
    assert resp.status_code == 204
    assert up.deletes == ["/api/v3/salesPrices/500"]


def test_delete_falls_back_to_v1():
    up = _Upstream(v3_available=False)
    resp = _call(up, method="DELETE", handle="ple_500", model=None)
    assert resp.status_code == 204
    assert up.deletes == ["/api/v1/salesPrices/500"]


def test_multi_tier_scale_price_is_writable():
    # A real Staffelpreis: three tiers differing only in minQuantity.
    up = _Upstream()
    tiers = [(1, "12.00"), (10, "10.50"), (100, "9.00")]
    ids = []
    for qty, price in tiers:
        resp = _call(
            up,
            method="POST",
            handle=None,
            model={
                "product": {"id": "prd_12"},
                "minQuantity": qty,
                "unitPrice": {"amount": price, "currency": "EUR"},
            },
        )
        assert resp.status_code == 201
        ids.append(json.loads(resp.content)["data"]["id"])
    # three distinct rows, each posted to v3 with its own quantity tier
    assert [b["quantity"] for _, b in up.posts] == [1, 10, 100]
    assert len(set(ids)) == 3


# ---- customer-group guard ------------------------------------------------
def _group_model(gid: str) -> dict[str, Any]:
    return {
        "product": {"id": "prd_12"},
        "scope": {"customerGroup": gid},
        "minQuantity": 1,
        "unitPrice": {"amount": "9.00", "currency": "EUR"},
    }


def test_group_price_rejects_unknown_group():
    # Upstream salesPrices would silently store a price against a ghost group id;
    # the guard rejects it up front and never posts.
    up = _Upstream()
    resp = _call(up, method="POST", handle=None, model=_group_model("1"))
    assert resp.status_code == 400
    assert "1" in json.loads(resp.content)["title"]
    assert up.posts == []  # nothing written upstream
    assert up.group_reads == 1


def test_group_price_accepts_known_group():
    up = _Upstream()
    resp = _call(up, method="POST", handle=None, model=_group_model("11"))
    assert resp.status_code == 201
    assert up.posts[0][0] == "/api/v3/salesPrices"
    assert up.posts[0][1]["customerGroup"] == {"id": "11"}


def test_group_guard_fails_open_when_groups_unreadable():
    # A flaky/forbidden groups read must never block a price write.
    up = _Upstream(groups_status=500)
    resp = _call(up, method="POST", handle=None, model=_group_model("1"))
    assert resp.status_code == 201  # fail-open: the write proceeds
    assert up.posts[0][1]["customerGroup"] == {"id": "1"}


def test_group_guard_runs_on_dryrun():
    # bulk_validate uses dryRun; the guard must catch an unknown group there too.
    up = _Upstream()
    a = PriceListAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="POST",
                handle=None,
                query=[("dryRun", "true")],
                body=json.dumps(_group_model("1")).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    resp = asyncio.run(go())
    assert resp.status_code == 400
    assert up.posts == []


def test_customer_price_skips_group_guard():
    # A customer-scoped (or standard) price must not pay the groups lookup.
    up = _Upstream()
    resp = _call(up, method="POST", handle=None, model=_ENTRY)  # customer scope
    assert resp.status_code == 201
    assert up.group_reads == 0
