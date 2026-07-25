"""agentos_neo_weclapp — engine unit tests (no live tenant).

Covers the tenant-independent translation layer (query → weclapp params, weclapp
record → our record, metadata contract) plus the request path end-to-end over a
mocked httpx transport, asserting the *effect* (the outgoing weclapp query
params and the response envelope), not just a 200.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx

from xentral_entity_cores.agentos_neo_weclapp.emulated import build_adapters
from xentral_entity_cores.agentos_neo_weclapp.emulated import base as wc_base
from xentral_entity_cores.agentos_neo_weclapp.emulated.base import (
    CoreCredentialsMissing,
    WECLAPP_FIELDS,
    build_list_params,
    parse_query,
    transform_record,
)

ADAPTERS = {a.manifest.key: a for a in build_adapters()}
CUSTOMER = ADAPTERS["Customer"]
ENTITY = CUSTOMER.entity
SALES_ORDER = ADAPTERS["SalesOrder"]

_EXPECTED_ROSTER = {
    "Customer",
    "Supplier",
    "Article",
    "Quotation",
    "SalesOrder",
    "SalesInvoice",
    "CreditNote",
    "PurchaseOrder",
    "Shipment",
}


def test_roster_covers_erp_core_entities():
    assert set(ADAPTERS) == _EXPECTED_ROSTER
    # every entity is read-only in v1 and carries a stable adapter marker
    for key, ad in ADAPTERS.items():
        assert ad.manifest.operations == ("list", "read")
        assert ad.manifest.adapter == f"agentos_neo_weclapp.{key}"
    assert ENTITY.endpoint == "party"


def test_all_entities_metadata_renders():
    """Every adapter renders a valid, non-empty schema offline (static schema)."""
    for ad in ADAPTERS.values():
        meta = ad.metadata()
        assert meta["origin"] == "emulated"
        assert meta["operations"] == ["list", "read"]
        assert isinstance(meta["rootNode"]["properties"], dict)
        assert meta["rootNode"]["properties"]  # non-empty


def test_supplier_slices_party_by_role():
    supplier = ADAPTERS["Supplier"].entity
    assert supplier.endpoint == "party"
    params = build_list_params(supplier, parse_query([]))
    assert params["supplier-eq"] == "true"
    assert "customer-eq" not in params


def test_sales_order_document_shape():
    props = SALES_ORDER.metadata()["rootNode"]["properties"]
    # consistent document model keys across documents
    assert props["documentNumber"]["previewable"] is True
    assert props["documentDate"]["type"] == "date"
    assert props["status"]["filterable"] is True
    # party reference -> Customer
    assert props["party"]["type"] == "reference"
    assert props["party"]["reference"] == "Customer"
    # order items collection with a nested article reference + quantity
    items = props["orderItems"]
    assert items["type"] == "collection"
    node = items["node"]["properties"]
    assert node["articleId"]["type"] == "reference" and node["articleId"]["reference"] == "Article"
    assert node["quantity"]["type"] == "decimal"
    # embedded delivery address
    assert props["deliveryAddress"]["type"] == "embedded"
    assert "city" in props["deliveryAddress"]["properties"]


def test_sales_order_transform_with_order_items():
    e = SALES_ORDER.entity
    raw = {
        "id": 900,
        "salesOrderNumber": "SO-42",
        "status": "OPEN",
        "orderDate": 1_700_000_000_000,
        "customerId": 4711,
        "netAmount": "100.00",
        "deliveryAddress": {"city": "Berlin", "countryCode": "DE"},
        "orderItems": [
            {"id": 5, "positionNumber": 1, "articleId": 7, "quantity": "2", "unitPrice": "50.00"},
        ],
        "internalNoise": "dropped",
    }
    out = transform_record(e, raw)
    assert out["documentNumber"] == "SO-42"
    assert out["documentDate"] == "2023-11-14"  # epoch ms -> ISO date (date type)
    assert out["party"] == {"id": "4711"}  # customerId -> reference
    assert out["deliveryAddress"]["city"] == "Berlin"
    item = out["orderItems"][0]
    assert item["id"] == "5" and item["articleId"] == {"id": "7"} and item["quantity"] == "2"
    assert "internalNoise" not in out


def test_metadata_contract_shape():
    meta = CUSTOMER.metadata()
    assert meta["key"] == "Customer"
    assert meta["origin"] == "emulated"
    assert meta["operations"] == ["list", "read"]
    props = meta["rootNode"]["properties"]
    # select carries options; datetime + epoch renders as datetime type
    assert props["partyType"]["type"] == "select"
    assert {o["value"] for o in props["partyType"]["options"]} == {"ORGANIZATION", "PERSON"}
    assert props["createdDate"]["type"] == "datetime"
    # reference shape
    assert props["currencyId"]["type"] == "reference"
    assert props["currencyId"]["reference"] == "Currency"
    assert props["currencyId"]["access"] == "readOnly"
    # collection nests under node.properties
    assert props["addresses"]["type"] == "collection"
    assert "city" in props["addresses"]["node"]["properties"]
    # preview + filterOperators on a filterable field
    assert props["customerNumber"]["previewable"] is True
    assert props["customerNumber"]["previewOrder"] == 0
    assert "contains" in props["customerNumber"]["filterOperators"]
    # everything is read-only in Phase 1
    assert all(p.get("access") == "readOnly" for p in props.values() if p["type"] != "collection")


def test_parse_query():
    parsed = parse_query(
        [
            ("filter[0][key]", "company"),
            ("filter[0][op]", "contains"),
            ("filter[0][value]", "acme"),
            ("sort", "-createdDate"),
            ("page[size]", "25"),
            ("page[number]", "3"),
        ]
    )
    assert parsed.filters == (("company", "contains", "acme"),)
    assert parsed.sort == "-createdDate"
    assert parsed.page == 3 and parsed.page_size == 25


def test_build_list_params_translates_ops_and_slice():
    parsed = parse_query(
        [
            ("filter[0][key]", "customerNumber"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "C-100"),
            ("filter[1][key]", "company"),
            ("filter[1][op]", "contains"),
            ("filter[1][value]", "acme"),
            ("filter[2][key]", "supplier"),
            ("filter[2][op]", "equals"),
            ("filter[2][value]", "no"),
            ("sort", "-createdDate"),
            ("page[size]", "10"),
            ("page[number]", "2"),
        ]
    )
    params = build_list_params(ENTITY, parsed)
    assert params["customer-eq"] == "true"  # base slice always present
    assert params["customerNumber-eq"] == "C-100"
    assert params["company-ilike"] == "%acme%"  # contains -> ilike + wildcards
    assert params["supplier-eq"] == "false"  # boolean coerced to string
    assert params["sort"] == "-createdDate"
    assert params["page"] == "2" and params["pageSize"] == "10"
    assert params["additionalProperties"] == "addresses"


def test_build_list_params_drops_unknown_and_nonsortable():
    parsed = parse_query(
        [
            ("filter[0][key]", "phone"),  # declared but not filterable
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "123"),
            ("filter[1][key]", "bogus"),  # unknown
            ("filter[1][op]", "equals"),
            ("filter[1][value]", "x"),
            ("sort", "phone"),  # not sortable
        ]
    )
    params = build_list_params(ENTITY, parsed)
    assert "phone-eq" not in params
    assert not any(k.startswith("bogus") for k in params)
    assert "sort" not in params


def test_build_list_params_count_omits_paging():
    parsed = parse_query([("sort", "-createdDate"), ("page[size]", "10")])
    params = build_list_params(ENTITY, parsed, for_count=True)
    assert "sort" not in params and "page" not in params and "pageSize" not in params
    assert params["customer-eq"] == "true"


def test_transform_record():
    raw = {
        "id": 4711,
        "customerNumber": "C-100",
        "company": "Acme GmbH",
        "partyType": "ORGANIZATION",
        "email": "info@acme.example",
        "createdDate": 1_700_000_000_000,  # epoch ms
        "currencyId": 55,
        "addresses": [
            {"id": 9, "city": "Berlin", "countryCode": "DE", "primeAddress": True},
        ],
        "undeclaredInternalField": "should not leak",
    }
    out = transform_record(ENTITY, raw)
    assert out["id"] == "4711" and out["uuid"] == "4711"
    assert out["company"] == "Acme GmbH"
    assert out["createdDate"].startswith("2023-11-14T")  # epoch ms -> ISO datetime
    assert out["currencyId"] == {"id": "55"}  # FK -> reference object
    assert out["addresses"][0]["city"] == "Berlin" and out["addresses"][0]["id"] == "9"
    assert "undeclaredInternalField" not in out  # only declared fields survive


# ---- request path over a mocked transport -----------------------------------


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fake_creds(monkeypatch):
    monkeypatch.setattr(
        wc_base,
        "resolve_core_credentials",
        lambda *a, **k: {
            "weclapp_base_url": "https://x.weclapp.com",
            "weclapp_api_token": "tok",
        },
    )


def test_list_end_to_end_effects(monkeypatch):
    _fake_creds(monkeypatch)
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        assert request.headers["AuthenticationToken"] == "tok"
        if request.url.path.endswith("/party/count"):
            seen["count"] = q
            return httpx.Response(200, json={"result": 2})
        if request.url.path.endswith("/party"):
            seen["list"] = q
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 1, "company": "A", "createdDate": 1_700_000_000_000},
                        {"id": 2, "company": "B"},
                    ]
                },
            )
        return httpx.Response(404, json={})

    async def run():
        async with _mock_client(handler) as client:
            return await CUSTOMER.request(
                method="GET",
                handle=None,
                query=[
                    ("filter[0][key]", "company"),
                    ("filter[0][op]", "contains"),
                    ("filter[0][value]", "a"),
                    ("page[size]", "10"),
                ],
                body=None,
                base_url="",
                token="",
                client=client,
            )

    resp = asyncio.run(run())
    assert resp.status_code == 200
    payload = json.loads(resp.content)
    # effect: both count and list were sliced to customers and carried the filter
    assert seen["count"]["customer-eq"] == "true"
    assert seen["count"]["company-ilike"] == "%a%"
    assert seen["list"]["pageSize"] == "10"
    assert "sort" not in seen["count"]  # count omits paging/sort
    # envelope: meta.total AND extra.total both present; records transformed
    assert payload["meta"]["total"] == 2 and payload["extra"]["total"] == 2
    assert payload["meta"]["count"] == 2
    assert payload["data"][0]["createdDate"].startswith("2023-11-14T")
    assert payload["data"][0]["id"] == "1"


def test_read_404(monkeypatch):
    _fake_creds(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async def run():
        async with _mock_client(handler) as client:
            return await CUSTOMER.request(
                method="GET",
                handle="999999",
                query=[],
                body=None,
                base_url="",
                token="",
                client=client,
            )

    resp = asyncio.run(run())
    assert resp.status_code == 404


def test_credentials_missing_returns_424(monkeypatch):
    def _raise(*a, **k):
        raise CoreCredentialsMissing(wc_base.CORE_ID, WECLAPP_FIELDS, ["weclapp_base_url"])

    monkeypatch.setattr(wc_base, "resolve_core_credentials", _raise)

    async def run():
        async with _mock_client(lambda r: httpx.Response(200, json={"result": []})) as client:
            return await CUSTOMER.request(
                method="GET",
                handle=None,
                query=[],
                body=None,
                base_url="",
                token="",
                client=client,
            )

    resp = asyncio.run(run())
    assert resp.status_code == 424
    body = json.loads(resp.content)
    assert body["code"] == "core_credentials_missing"
    assert body["core"] == "agentos_neo_weclapp"


def test_write_is_405(monkeypatch):
    _fake_creds(monkeypatch)

    async def run():
        async with _mock_client(lambda r: httpx.Response(200, json={})) as client:
            return await CUSTOMER.request(
                method="POST",
                handle=None,
                query=[],
                body=b"{}",
                base_url="",
                token="",
                client=client,
            )

    resp = asyncio.run(run())
    assert resp.status_code == 405
