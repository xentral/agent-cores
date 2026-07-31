"""Line-item purchasePrice (EK) is writable on offer, salesOrder, invoice, creditNote.

Upstream made the position's own cost price writable on create + update for exactly
these four document types (proforma has no EK columns). These pin:
  * the schema declares ``items.purchasePrice`` creatable everywhere, and updatable
    ONLY on salesOrder — the other three reject ``items`` on UPDATE, so an
    ``updatable`` flag there would promise an edit the core does not perform;
  * map_write emits the v3 ``{"net": {amount, currency}}`` shape, and an omitted EK
    is never sent as null (upstream's EK columns are NOT NULL);
  * the currency falls back to the DOCUMENT currency, not a bare "EUR" — upstream
    rejects an EK whose currency differs from the document's;
  * map_read maps the EK back into the model's flat money shape;
  * the salesOrder reconcile PATCHes the EK using the currency of the fetched order;
  * splitOrder carries a manual EK onto the partial order.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

_ALL = (QuoteAdapter, SalesOrderAdapter, SalesInvoiceAdapter, CreditNoteAdapter)


def _item_schema(adapter_cls: type) -> dict[str, Any]:
    return adapter_cls().fields()["items"]["node"]["properties"]


# ---- schema -------------------------------------------------------------


def test_purchase_price_declared_creatable_on_all_four():
    for cls in _ALL:
        pp = _item_schema(cls)["purchasePrice"]
        assert pp["type"] == "embedded", cls.__name__
        assert pp.get("creatable") is True, cls.__name__
        assert set(pp["properties"]) == {"amount", "currency"}, cls.__name__


def test_updatable_only_where_items_are_actually_reconciled():
    # salesOrder reconciles its positions against the v3 lineItems sub-resource…
    assert _item_schema(SalesOrderAdapter)["purchasePrice"].get("updatable") is True
    # …the other three reject `items` on UPDATE, so they must not claim otherwise.
    for cls in (QuoteAdapter, SalesInvoiceAdapter, CreditNoteAdapter):
        assert "updatable" not in _item_schema(cls)["purchasePrice"], cls.__name__
        assert cls().map_write({"items": [{"product": {"id": "prd_1"}}]}, creating=False)[1] == {
            "items"
        }, cls.__name__


# ---- write --------------------------------------------------------------


def _create_line(cls: type, item: dict[str, Any], **doc: Any) -> dict[str, Any]:
    body, _rejected = cls().map_write({"items": [item], **doc}, creating=True)
    return body["lineItems"][0]


def test_map_write_emits_v3_purchase_price_shape():
    for cls in _ALL:
        line = _create_line(
            cls,
            {
                "product": {"id": "prd_61988"},
                "quantity": {"value": 1},
                "purchasePrice": {"amount": "3.33", "currency": "EUR"},
            },
        )
        assert line["purchasePrice"] == {"net": {"amount": "3.33", "currency": "EUR"}}, cls.__name__


def test_absent_purchase_price_is_never_sent_as_null():
    # upstream's einkaufspreis columns are NOT NULL — a cleared EK must not be emitted
    for cls in _ALL:
        line = _create_line(cls, {"product": {"id": "prd_1"}, "quantity": {"value": 1}})
        assert "purchasePrice" not in line, cls.__name__
        line = _create_line(cls, {"product": {"id": "prd_1"}, "purchasePrice": None})
        assert "purchasePrice" not in line, cls.__name__


def test_currency_falls_back_to_the_document_not_eur():
    """A bare amount on a USD document must go out as USD; sending EUR would earn a
    400 from upstream ('currency of the purchase price must match the document')."""
    for cls in _ALL:
        line = _create_line(
            cls,
            {
                "product": {"id": "prd_1"},
                "purchasePrice": {"amount": 5},
                "unitPrice": {"amount": 9},
            },
            currency="USD",
        )
        assert line["purchasePrice"] == {"net": {"amount": "5", "currency": "USD"}}, cls.__name__
        assert line["price"] == {"net": {"amount": "9", "currency": "USD"}}, cls.__name__


def test_explicit_currency_wins_over_the_document():
    line = _create_line(
        SalesOrderAdapter,
        {"product": {"id": "prd_1"}, "purchasePrice": {"amount": 5, "currency": "CHF"}},
        currency="USD",
    )
    assert line["purchasePrice"] == {"net": {"amount": "5", "currency": "CHF"}}


# ---- read ---------------------------------------------------------------


def test_map_read_surfaces_the_purchase_price():
    raw = {
        "id": 1,
        "financials": {"currency": "EUR"},
        "lineItems": [
            {
                "id": 151010,
                "order": 1,
                "product": {"id": "61988"},
                "quantity": 1,
                "purchasePrice": {"net": {"amount": "7.77000000", "currency": "EUR"}},
            }
        ],
    }
    for cls in _ALL:
        item = cls().map_read(raw)["items"][0]
        assert item["purchasePrice"] == {"amount": "7.77", "currency": "EUR"}, cls.__name__


def test_map_read_reports_none_when_upstream_has_no_ek():
    raw = {
        "id": 1,
        "financials": {"currency": "EUR"},
        "lineItems": [{"id": 1, "order": 1, "product": {"id": "1"}, "quantity": 1}],
    }
    for cls in _ALL:
        assert cls().map_read(raw)["items"][0]["purchasePrice"] is None, cls.__name__


# ---- salesOrder reconcile ------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def request(self, method: str, url: str, json: Any = None, headers: dict | None = None):  # noqa: A002
        self.calls.append((method, url, json))
        if method == "POST":
            return _Resp(201, {"data": {"id": "150999"}})
        if method == "PATCH":
            return _Resp(200, {"data": {"id": url.rsplit("/", 1)[-1]}})
        return _Resp(204, {})


def _adapter(order: dict) -> SalesOrderAdapter:
    a = SalesOrderAdapter()

    async def _fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        return (200, {"data": order})

    a._get = _fake_get  # type: ignore[method-assign]
    return a


def _order(currency: str = "EUR") -> dict:
    return {
        "id": 22911,
        "documentNumber": "AB-1",
        "financials": {"currency": currency},
        "lineItems": [
            {"id": 150975, "order": 1, "product": {"id": "1"}, "quantity": 1, "unit": "piece"}
        ],
    }


def _patch_items(adapter: SalesOrderAdapter, client: _FakeClient, items: list[dict]):
    return asyncio.run(
        adapter.request(
            method="PATCH",
            handle="so_22911",
            query=[],
            body=json.dumps({"items": items}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=client,
        )
    )


def test_reconcile_patches_the_purchase_price():
    client = _FakeClient()
    resp = _patch_items(
        _adapter(_order()),
        client,
        [{"id": "150975", "purchasePrice": {"amount": "7.77", "currency": "EUR"}}],
    )
    assert resp.status_code == 200
    body = next(b for m, _u, b in client.calls if m == "PATCH")
    assert body["purchasePrice"] == {"net": {"amount": "7.77", "currency": "EUR"}}


def test_reconcile_uses_the_fetched_orders_currency():
    """The PATCH path has no model-level currency — it must read the document's own
    rather than defaulting the EK to EUR on a USD order."""
    client = _FakeClient()
    _patch_items(
        _adapter(_order("USD")),
        client,
        [{"id": "150975", "purchasePrice": {"amount": "7.77"}}],
    )
    body = next(b for m, _u, b in client.calls if m == "PATCH")
    assert body["purchasePrice"] == {"net": {"amount": "7.77", "currency": "USD"}}


def test_split_order_carries_the_manual_ek_onto_the_partial():
    """Without this the partial re-derives the EK from the price list and the
    contribution margin silently shifts."""
    order = _order()
    order["lineItems"][0].update(
        {
            "quantity": 5,
            "price": {"net": {"amount": "100.00", "currency": "EUR"}},
            "purchasePrice": {"net": {"amount": "3.33", "currency": "EUR"}},
        }
    )
    adapter = _adapter(order)
    calls: list[tuple[str, str, Any]] = []

    async def _fake_create_partial(src_up, base_url, token, accept_language, client):  # noqa: ANN001, ANN202
        return "22999", 201, {}

    async def _fake_li_call(method, url, token, al, client, payload=None):  # noqa: ANN001, ANN202
        calls.append((method, url, payload))
        return {"POST": 201, "PATCH": 200, "DELETE": 204}.get(method, 200), {"data": {"id": "999"}}

    adapter._create_partial = _fake_create_partial  # type: ignore[method-assign]
    adapter._li_call = _fake_li_call  # type: ignore[method-assign]

    resp = asyncio.run(
        adapter._split_order(
            "so_22911",
            json.dumps(
                {"ids": ["so_22911"], "command": {"items": [{"lineItem": "150975", "quantity": 2}]}}
            ).encode(),
            "https://x.test",
            "t",
            None,
            None,
        )
    )
    assert resp.status_code < 400, resp.body
    added = [b for m, u, b in calls if m == "POST" and u.endswith("/22999/lineItems")]
    assert added, [(m, u) for m, u, _ in calls]
    assert added[0]["purchasePrice"] == {"net": {"amount": "3.33", "currency": "EUR"}}
