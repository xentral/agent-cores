"""Settings lookups (settings.py): query dialects, mapping, bespoke adapters.

The three upstream query dialects must reach the wire exactly as the public
OpenAPI declares them (a stray ``sort`` or ``page[…]`` key is a 400 on the
strict v1/v2 endpoints), TaxRate must fold the ``country`` filter into the
path, TextTemplate must unfold the one settings object into per-type records,
and Channel must be enriched with active/platform from /api/v2/salesChannels.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl, urlsplit

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.channel import ChannelAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.settings import (
    SETTINGS_ADAPTERS,
    EmployeeAdapter,
    PaymentMethodAdapter,
    ProductCategoryAdapter,
    TaxRateAdapter,
    TextTemplateAdapter,
)
from xentral_entity_cores.agentos_neo_xentral.manifest import CORE

BASE = "https://tenant.example"


class Upstream:
    """Records every request and answers from a {path: payload} table."""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = self.routes.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={"title": f"no route {request.url.path}"})
        return httpx.Response(200, json=payload)


def _run(adapter, upstream: Upstream, *, method="GET", handle=None, query=()):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)) as client:
            return await adapter.request(
                method=method,
                handle=handle,
                query=list(query),
                body=None,
                base_url=BASE,
                token="t",
                client=client,
            )

    return asyncio.run(go())


def _body(resp) -> dict:
    return json.loads(resp.content)


def _params(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(request.url)).query)


# ---- query dialects -------------------------------------------------------


def test_paged_dialect_sends_page_and_filters_only():
    up = Upstream({"/api/v1/paymentMethods": {"data": [], "extra": {"totalCount": 0}}})
    _run(
        PaymentMethodAdapter(),
        up,
        query=[
            ("page[number]", "2"),
            ("page[size]", "25"),
            ("sort", "-createdAt"),
            ("filter[0][key]", "type"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "paypal"),
        ],
    )
    params = _params(up.requests[0])
    keys = [k for k, _ in params]
    assert ("page[number]", "2") in params
    assert ("filter[0][value]", "paypal") in params
    assert "sort" not in keys  # strict v1 endpoints reject unexpected keys


def test_none_dialect_sends_no_query_at_all():
    up = Upstream({"/api/v1/productsCategories": {"data": []}})
    _run(
        ProductCategoryAdapter(),
        up,
        query=[("page[number]", "1"), ("page[size]", "25"), ("sort", "name")],
    )
    assert _params(up.requests[0]) == []


def test_filters_dialect_sends_filters_but_no_page():
    up = Upstream({"/api/v1/employees": {"data": []}})
    _run(
        EmployeeAdapter(),
        up,
        query=[
            ("page[number]", "1"),
            ("page[size]", "25"),
            ("filter[0][key]", "name"),
            ("filter[0][op]", "contains"),
            ("filter[0][value]", "Muster"),
        ],
    )
    params = _params(up.requests[0])
    keys = [k for k, _ in params]
    assert ("filter[0][value]", "Muster") in params
    assert not any(k.startswith("page[") for k in keys)


# ---- mapping + write gate -------------------------------------------------


def test_payment_method_mapping_and_total():
    up = Upstream(
        {
            "/api/v1/paymentMethods": {
                "data": [
                    {"id": "6", "type": "amazon", "designation": "Amazon", "project": {"id": "1"}}
                ],
                "extra": {"totalCount": 1},
            }
        }
    )
    body = _body(_run(PaymentMethodAdapter(), up))
    rec = body["data"][0]
    assert rec["id"] == "paym_6"
    assert rec["name"] == "Amazon"
    assert rec["project"]["id"] == "prj_1"
    assert body["extra"]["total"] == 1


def test_read_only_entities_reject_writes_with_405():
    up = Upstream({})
    resp = _run(PaymentMethodAdapter(), up, method="POST")
    assert resp.status_code == 405
    assert up.requests == []  # never reached the upstream


def test_list_only_entities_reject_read_with_405():
    up = Upstream({})
    resp = _run(PaymentMethodAdapter(), up, handle="paym_6")
    assert resp.status_code == 405
    assert up.requests == []


# ---- TaxRate: country path + positional ids ------------------------------


def test_tax_rate_defaults_to_de_and_synthesizes_ids():
    up = Upstream(
        {
            "/api/v1/taxRates/DE": {
                "data": [
                    {"rate": "19.00", "type": "standard", "name": "Normal", "date": "2026-01-01"},
                    {"rate": "7.00", "type": "reduced", "name": "Reduced", "date": "2026-01-01"},
                ]
            }
        }
    )
    body = _body(_run(TaxRateAdapter(), up))
    assert [r["id"] for r in body["data"]] == ["tax_DE_1", "tax_DE_2"]
    assert body["data"][0]["country"] == "DE"
    assert body["data"][0]["rate"] == "19.00"


def test_tax_rate_country_filter_selects_path_and_aliases_product():
    up = Upstream({"/api/v1/taxRates/AT": {"data": []}})
    _run(
        TaxRateAdapter(),
        up,
        query=[
            ("filter[0][key]", "country"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "at"),
            ("filter[1][key]", "product"),
            ("filter[1][op]", "equals"),
            ("filter[1][value]", "prd_61617"),
        ],
    )
    req = up.requests[0]
    assert req.url.path == "/api/v1/taxRates/AT"
    params = dict(_params(req))
    assert params["filter[0][key]"] == "productId"  # re-indexed after country removal
    assert params["filter[0][value]"] == "61617"  # speaking prefix stripped


# ---- TextTemplate: object → records ---------------------------------------


_TT_PAYLOAD = {
    "data": {
        "offer": {"header": "H", "footer": "F", "disable_stationary": False},
        "order": {"header": "OH", "footer": "OF", "disable_stationary": True},
        "invoice": {"header": None, "footer": None, "disable_stationary": False},
        "delivery_note": {},
        "credit_note": {},
        "purchase_order": {},
        "work_report": {},
        "commission_credit_note": {},
        "proforma_invoice": {},
        "misc": {"travel_expenses_without_letterhead": True},
    }
}


def test_text_templates_unfold_into_records():
    up = Upstream({"/api/v2/settings/text-templates": _TT_PAYLOAD})
    body = _body(_run(TextTemplateAdapter(), up))
    ids = [r["id"] for r in body["data"]]
    assert "tpl_offer" in ids and "tpl_delivery_note" in ids
    assert "tpl_misc" not in ids  # differently-shaped block is not exposed
    order = next(r for r in body["data"] if r["id"] == "tpl_order")
    assert order["disableStationery"] is True


def test_text_template_read_by_handle():
    up = Upstream({"/api/v2/settings/text-templates": _TT_PAYLOAD})
    body = _body(_run(TextTemplateAdapter(), up, handle="tpl_offer"))
    assert body["data"]["documentType"] == "offer"
    assert body["data"]["header"] == "H"
    assert _run(TextTemplateAdapter(), up, handle="tpl_nope").status_code == 404


# ---- Channel enrichment ----------------------------------------------------


def test_channel_list_enriched_with_active_and_platform():
    up = Upstream(
        {
            "/api/entity/salesChannel": {
                "data": [{"id": "3", "uuid": "u-3", "name": "Shopify DE"}]
            },
            "/api/v2/salesChannels": {
                "data": [
                    {"id": "3", "moduleName": "shopimporter_shopify", "active": True},
                ]
            },
        }
    )
    body = _body(_run(ChannelAdapter(), up))
    rec = body["data"][0]
    assert rec["active"] is True
    assert rec["platform"] == "shopify"


def test_channel_survives_v2_enrichment_failure():
    up = Upstream(
        {"/api/entity/salesChannel": {"data": [{"id": "3", "uuid": "u-3", "name": "POS"}]}}
    )
    body = _body(_run(ChannelAdapter(), up))
    assert body["data"][0]["name"] == "POS"
    assert body["data"][0]["active"] is None  # unenriched, not wrong


# ---- registration invariants ----------------------------------------------


def test_all_settings_adapters_registered_in_core():
    registered = {a.manifest.key for a in CORE.adapters}
    assert {a.manifest.key for a in SETTINGS_ADAPTERS} <= registered


def test_entity_keys_unique_across_core():
    keys = [a.manifest.key for a in CORE.adapters]
    assert len(keys) == len(set(keys))


def test_settings_metadata_builds():
    for adapter in SETTINGS_ADAPTERS:
        meta = adapter.metadata()
        assert meta["key"] == adapter.manifest.key
        assert meta["rootNode"]["properties"]["id"]
