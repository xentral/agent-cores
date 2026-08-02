"""Product write: v2 create/update mapping + sale-price composition.

The Product entity reads the purpose-built v3 read model but must WRITE through
v2 products (``POST/PATCH /api/v2/products``) — the first adapter whose write path
differs from its read path. Sale price is not part of the product body upstream;
it is a separate ``salesPrices`` resource composed on top of a successful create.

These pin:
  * map_write → the v2 create body (field names grounded in the v2 OpenAPI schema),
    with ``prices.sale`` deliberately NOT in the body (composed separately),
    read-only fields ignored (not rejected, so round-trip writes work), and a
    default project injected only on create;
  * the full write flow through MockTransport: POST hits /api/v2/products (not v3),
    the sale price is posted to /api/v3/salesPrices (v1 salesPrices as fallback),
    and the response carries it;
  * the honest partial-success path when the product is created but the price fails.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter

_FULL_MODEL: dict[str, Any] = {
    "name": "Alu-Flasche 750 ml",
    "number": "SKU-100",
    "description": "Trinkflasche",
    "unit": "piece",
    "category": {"id": "mg_7"},
    "project": {"id": "prj_3"},
    "identifiers": {
        "ean": "4001234567890",
        "hsCode": "76129080",
        "countryOfOrigin": "DE",
        "manufacturerNumber": "ALB-750-S",  # → v2 manufacturer.number
    },
    "manufacturer": {"name": "AluBottle Inc.", "website": "https://alu.example"},
    "prices": {
        "purchase": {"amount": "6.20", "currency": "EUR", "source": "manual"},
        "sale": {"amount": "12.90", "currency": "EUR"},
    },
    "tax": {"rate": "reduced"},
    "logistics": {
        "weight": {"value": 0.18, "unit": "kg"},
        "dimensions": {"length": 8, "width": 8, "height": 26, "unit": "cm"},
        "minimumOrderQuantity": 1,
        "minimumStockQuantity": 50,
    },
    "tracking": {"stock": True, "batches": True, "serialNumbers": "none", "bestBefore": True},
    "production": {"mode": "inHouse", "hasBillOfMaterials": False},
    "kind": "physical",
    "documentDefaults": {"hidePrice": False, "noticeText": "internal"},
    "variant": {"isMatrix": False},
    "suppliers": [{"supplier": {"id": "sup_42"}, "isDefault": True}],
    # read-only / computed fields the read emits — must be ignored, not rejected
    "status": "active",
    "stock": {"available": 5},
    "tags": ["b2b"],
    "customFields": {},
    "object": "product",
    "id": "prd_1",
}


def test_operations_and_write_path():
    """Three upstream generations at once, which is why the delete needs its own
    path: v2 has no DELETE route (measured: 404 "Route not found") while v1 answers
    204 and the record is gone on a re-read."""
    a = ProductAdapter()
    assert set(a.manifest.operations) == {"list", "read", "create", "update", "delete"}
    assert a.v3_path == "/api/v3/products"  # reads stay v3
    assert a.write_path == "/api/v2/products"  # writes go to v2
    assert a.delete_path == "/api/products"  # …and the delete to v1


def test_a_delete_is_routed_to_the_delete_path():
    """The base and Product's own `_send` override both have to honour it —
    restating only the write path in the override is how the first attempt at
    declaring `delete` silently 404'd against a route that does not exist."""
    import asyncio

    import httpx

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(204)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await ProductAdapter().request(
                method="DELETE",
                handle="prd_42",
                query=[],
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    asyncio.run(go())
    assert seen == [("DELETE", "/api/products/42")], seen


def test_map_write_full_model_to_v2_body():
    v2, rejected = ProductAdapter().map_write(_FULL_MODEL, creating=True)
    assert rejected == set()  # every key is writable or a known-ignored read field

    # scalars + references (speaking prefixes stripped to the bare upstream id)
    assert v2["name"] == "Alu-Flasche 750 ml"
    assert v2["number"] == "SKU-100"
    assert v2["merchandiseGroup"] == {"id": "7"}
    assert v2["project"] == {"id": "3"}
    assert v2["ean"] == "4001234567890"
    assert v2["customsTariffNumber"] == "76129080"
    assert v2["countryOfOrigin"] == "DE"
    # manufacturer number nests under manufacturer.number (from identifiers.manufacturerNumber)
    assert v2["manufacturer"] == {
        "name": "AluBottle Inc.",
        "link": "https://alu.example",
        "number": "ALB-750-S",
    }
    assert v2["salesTax"] == "reduced"
    assert v2["standardSupplier"] == {"id": "42"}

    # purchase price (manual → hasCalculatedPurchasePrice false)
    assert v2["calculatedPurchasePrice"] == {
        "hasCalculatedPurchasePrice": False,
        "price": {"amount": "6.20", "currency": "EUR"},
    }

    # measurements
    assert v2["measurements"]["weight"] == {"value": 0.18, "unit": "kg"}
    assert v2["measurements"]["length"] == {"value": 8, "unit": "cm"}
    assert v2["minimumOrderQuantity"] == 1
    assert v2["minimumStorageQuantity"] == 50

    # tracking + production flags
    assert v2["isStockItem"] is True
    assert v2["hasBatches"] is True
    assert v2["hasBestBeforeDate"] is True
    assert v2["serialNumbersMode"] == "disabled"  # model 'none' → v2 'disabled'
    assert v2["isProductionProduct"] is True
    assert v2["isAssembledJustInTime"] is False
    assert v2["hidePriceOnDocuments"] is False
    assert v2["isMatrixProduct"] is False


def test_sale_price_is_not_in_the_product_body():
    # Sale price is a separate resource; it must never leak into the v2 body.
    v2, _ = ProductAdapter().map_write(_FULL_MODEL, creating=True)
    assert "salesPrice" not in v2 and "prices" not in v2
    # the sale amount (12.90) must not be smuggled anywhere; only the purchase
    # price (6.20) belongs in the v2 body
    assert "12.90" not in json.dumps(v2)
    assert "6.20" in json.dumps(v2)
    # manufacturerNumber is written NESTED (manufacturer.number), never as a top-level key
    assert "manufacturerNumber" not in v2
    assert v2["manufacturer"]["number"] == "ALB-750-S"


def test_default_project_only_on_create():
    a = ProductAdapter()
    created, _ = a.map_write({"name": "X"}, creating=True)
    assert created["project"] == {"id": "1"}  # v2 requires a project on create
    updated, _ = a.map_write({"name": "X"}, creating=False)
    assert "project" not in updated  # update must not inject one


def test_unknown_key_is_rejected():
    _, rejected = ProductAdapter().map_write({"name": "X", "bogus": 1}, creating=True)
    assert rejected == {"bogus"}


def test_tax_exempt_maps_to_free():
    v2, _ = ProductAdapter().map_write({"tax": {"rate": "exempt"}}, creating=False)
    assert v2["salesTax"] == "free"


def test_tax_rate_reads_from_v3_taxrate_field():
    a = ProductAdapter()
    # v3 read exposes the rate as ``taxRate`` (not ``salesTax``); upstream "free"
    # is the model's "exempt". Verified live: writing tax.rate then reading back
    # returned None until map_read read ``taxRate``.
    assert a.map_read({"id": 1, "taxRate": "standard"})["tax"]["rate"] == "standard"
    assert a.map_read({"id": 1, "taxRate": "free"})["tax"]["rate"] == "exempt"
    # v2 view field name still works as a fallback
    assert a.map_read({"id": 1, "salesTax": "reduced"})["tax"]["rate"] == "reduced"
    assert a.map_read({"id": 1})["tax"]["rate"] is None


def test_map_read_uses_the_actual_v3_field_names():
    # These leaves regressed to null because map_read read the v2 write-names, not
    # the v3 read-names. Pin the correct v3 source fields (colleague-reported).
    a = ProductAdapter()
    rec = a.map_read(
        {
            "id": 1,
            "manufacturer": "ACME",
            "manufacturerUrl": "https://acme.example",
            "manufacturerProductNumber": "MFR-77",
            "minimumStockLevel": 25,
            "serialNumberTracking": "stockGenerated",
            "printSettings": {"withoutPrices": True},
        }
    )
    assert rec["manufacturer"] == {"name": "ACME", "website": "https://acme.example"}
    assert rec["identifiers"]["manufacturerNumber"] == "MFR-77"
    assert rec["logistics"]["minimumStockQuantity"] == 25
    assert rec["tracking"]["serialNumbers"] == "stockGenerated"
    assert rec["documentDefaults"]["hidePrice"] is True


def test_manufacturer_number_is_writable_and_nested():
    f = ProductAdapter().fields()
    assert f["identifiers"]["properties"]["manufacturerNumber"].get("creatable")
    v2, rejected = ProductAdapter().map_write(
        {"identifiers": {"manufacturerNumber": "MFR-77"}}, creating=False
    )
    assert rejected == set()
    assert v2["manufacturer"] == {"number": "MFR-77"}


def test_writable_fields_flagged_in_schema():
    f = ProductAdapter().fields()
    assert f["name"].get("creatable") and f["name"].get("updatable")
    assert f["number"].get("creatable") and not f["number"].get("updatable")  # create-only
    assert f["tracking"]["properties"]["stock"].get("creatable")
    assert f["prices"]["properties"]["sale"]["properties"]["amount"].get("creatable")
    # read-only fields carry no create/update capability
    assert f["stock"].get("access") == "readOnly"
    assert f["kind"].get("access") == "readOnly"


class _Upstream:
    """Stateful fake Xentral for the two-call product+price write."""

    def __init__(self, *, price_status: int = 201, v3_available: bool = True):
        self.price_status = price_status
        self.v3_available = v3_available
        self.product_posts: list[dict[str, Any]] = []
        self.price_posts: list[dict[str, Any]] = []
        self.post_paths: list[str] = []

    def _price_response(self, request: httpx.Request) -> httpx.Response:
        self.post_paths.append(request.url.path)
        self.price_posts.append(json.loads(request.content))
        if self.price_status >= 400:
            return httpx.Response(self.price_status, json={"title": "price rejected"})
        return httpx.Response(201, json={"data": {"id": "9"}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/products" and request.method == "POST":
            self.post_paths.append(path)
            self.product_posts.append(json.loads(request.content))
            return httpx.Response(201, json={"data": {"id": "777"}})
        if path == "/api/v3/products/777" and request.method == "GET":
            return httpx.Response(
                200, json={"data": {"id": 777, "name": "Alu-Flasche 750 ml", "number": "SKU-100"}}
            )
        # v3 salesPrices is the primary target; a tenant without the Beta endpoint
        # (or scope) answers 404, and the adapter falls back to v1 salesPrices.
        if path == "/api/v3/salesPrices" and request.method == "POST":
            if not self.v3_available:
                return httpx.Response(404, json={"title": "not found"})
            return self._price_response(request)
        if path == "/api/v1/salesPrices" and request.method == "POST":
            return self._price_response(request)
        raise AssertionError(f"unexpected call: {request.method} {path}")


def _create(upstream: _Upstream, model: dict[str, Any], query=None):
    a = ProductAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)) as client:
            return await a.request(
                method="POST",
                handle=None,
                query=query or [],
                body=json.dumps(model).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def test_create_flow_writes_v2_then_sale_price():
    up = _Upstream()
    resp = _create(up, _FULL_MODEL)
    assert resp.status_code == 201
    # product write goes to v2 products (NOT v3); the sale price goes to v3
    # salesPrices, where the quantity tier is named ``quantity`` (v1 calls it ``amount``)
    assert up.post_paths == ["/api/v2/products", "/api/v3/salesPrices"]
    assert up.product_posts[0]["name"] == "Alu-Flasche 750 ml"
    assert up.price_posts[0] == {
        "product": {"id": "777"},
        "quantity": 1,
        "price": {"amount": "12.90", "currency": "EUR"},
    }
    # the response carries the sale price we just persisted (v3 read omits it)
    data = json.loads(resp.content)["data"]
    assert data["id"] == "prd_777"
    assert data["prices"]["sale"] == {"amount": "12.90", "currency": "EUR"}
    assert "_warnings" not in data


def test_sale_price_falls_back_to_v1_when_v3_unavailable():
    # v3 salesPrices is Beta: a tenant without it answers 404, and the adapter
    # retries on v1 salesPrices (whose quantity tier is named ``amount``).
    up = _Upstream(v3_available=False)
    resp = _create(up, _FULL_MODEL)
    assert resp.status_code == 201
    assert up.post_paths == ["/api/v2/products", "/api/v1/salesPrices"]
    assert up.price_posts[0] == {
        "product": {"id": "777"},
        "amount": 1,
        "price": {"amount": "12.90", "currency": "EUR"},
    }
    data = json.loads(resp.content)["data"]
    assert data["prices"]["sale"] == {"amount": "12.90", "currency": "EUR"}
    assert "_warnings" not in data


def test_create_without_sale_price_makes_no_price_call():
    up = _Upstream()
    model = {k: v for k, v in _FULL_MODEL.items() if k != "prices"}
    model["prices"] = {"purchase": {"amount": "6.20", "currency": "EUR"}}
    resp = _create(up, model)
    assert resp.status_code == 201
    assert up.post_paths == ["/api/v2/products"]  # no second call
    assert up.price_posts == []


def test_partial_success_when_price_fails():
    up = _Upstream(price_status=400)
    resp = _create(up, _FULL_MODEL)
    # product was created — do NOT turn a created product into a hard error
    assert resp.status_code == 201
    data = json.loads(resp.content)["data"]
    assert data["id"] == "prd_777"
    warn = data["_warnings"]["salePrice"]
    assert warn["status"] == 400
    assert "sale price failed" in warn["message"]


def test_dry_run_creates_nothing():
    up = _Upstream()
    resp = _create(up, _FULL_MODEL, query=[("dryRun", "true")])
    assert resp.status_code == 200
    body = json.loads(resp.content)["data"]
    assert body["dryRun"] is True
    assert body["wouldSend"]["name"] == "Alu-Flasche 750 ml"
    assert up.post_paths == []  # nothing hit the network


def test_map_write_status_lock_and_reason():
    """status active/inactive → v2 isDisabled, statusReason → disabledReason;
    writable in a normal payload (so a bulk import can lock records), not rejected.
    archived is NOT emitted (v2 rejects isDeleted writes)."""
    a = ProductAdapter()
    v2, rejected = a.map_write(
        {"name": "X", "status": "inactive", "statusReason": "Sperrgrund"}, creating=False
    )
    assert rejected == set()
    assert v2["isDisabled"] is True
    assert "isDeleted" not in v2
    assert v2["disabledReason"] == "Sperrgrund"

    v2, _ = a.map_write({"name": "X", "status": "active"}, creating=False)
    assert v2["isDisabled"] is False and "isDeleted" not in v2
    assert v2["disabledReason"] is None  # reactivate (update) clears the reason

    # On CREATE, active is the default → emit nothing (v2 create rejects
    # disabledReason:null with 400).
    v2, rejected = a.map_write({"name": "X", "status": "active"}, creating=True)
    assert rejected == set()
    assert "isDisabled" not in v2 and "disabledReason" not in v2

    # archived can't be written via v2 → no flag emitted, round-trip stays a no-op.
    v2, rejected = a.map_write({"name": "X", "status": "archived"}, creating=False)
    assert rejected == set()
    assert "isDeleted" not in v2 and "isDisabled" not in v2


def test_status_steps_wired_to_v2():
    """deactivate/activate are executable (PATCH v2 isDisabled); archive stays a
    wish (v2 rejects isDeleted)."""
    a = ProductAdapter()
    assert set(a.action_map) == {"deactivate", "activate"}
    cmds = {c["key"]: c for c in a.steps()[0]["commands"]}
    assert not cmds["deactivate"].get("wish") and not cmds["activate"].get("wish")
    assert cmds["archive"].get("wish")
    assert a.action_map["deactivate"] == {
        "method": "PATCH",
        "path": "/api/v2/products/{id}",
        "body": {"isDisabled": True},
    }
    assert a.action_map["activate"]["body"] == {"isDisabled": False, "disabledReason": None}


def test_status_filter_maps_to_isdisabled():
    """A `status` filter is exposed and maps to the v3 isDisabled flag (default
    list is active-only), so callers can find inactive products."""
    a = ProductAdapter()
    assert a.fields()["status"].get("filterable") is True
    assert a.query_aliases.get("status") == "isDisabled"
    # inactive → isDisabled true, active → false (v3 wants the bool as a string).
    assert a.filter_value_maps["status"] == {"active": "false", "inactive": "true"}
