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
    the sale price is posted to /api/v1/salesPrices, and the response carries it;
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
        "manufacturerNumber": "ALB-750-S",  # read-only: no v2 slot
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
    a = ProductAdapter()
    assert set(a.manifest.operations) == {"list", "read", "create", "update"}
    assert a.v3_path == "/api/v3/products"  # reads stay v3
    assert a.write_path == "/api/v2/products"  # writes go to v2


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
    assert v2["manufacturer"] == {"name": "AluBottle Inc.", "link": "https://alu.example"}
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
    # manufacturerNumber has no v2 slot → silently skipped (schema marks it RO)
    assert "manufacturerNumber" not in v2


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

    def __init__(self, *, price_status: int = 201):
        self.price_status = price_status
        self.product_posts: list[dict[str, Any]] = []
        self.price_posts: list[dict[str, Any]] = []
        self.post_paths: list[str] = []

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
        if path == "/api/v1/salesPrices" and request.method == "POST":
            self.post_paths.append(path)
            self.price_posts.append(json.loads(request.content))
            if self.price_status >= 400:
                return httpx.Response(self.price_status, json={"title": "price rejected"})
            return httpx.Response(201, json={"data": {"id": "9"}})
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
    # write went to v2 products (NOT v3), then the sale price to salesPrices
    assert up.post_paths == ["/api/v2/products", "/api/v1/salesPrices"]
    assert up.product_posts[0]["name"] == "Alu-Flasche 750 ml"
    assert up.price_posts[0] == {
        "product": {"id": "777"},
        "amount": 1,
        "price": {"amount": "12.90", "currency": "EUR"},
    }
    # the response carries the sale price we just persisted (v3 read omits it)
    data = json.loads(resp.content)["data"]
    assert data["id"] == "prd_777"
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
