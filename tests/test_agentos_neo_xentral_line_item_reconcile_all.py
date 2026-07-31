"""Quote, invoice and creditNote reconcile their line items like salesOrder does.

Upstream exposes `{collection}/{id}/lineItems` (POST/PATCH/DELETE) for offers,
invoices and creditNotes just as it does for salesOrders, but only salesOrder used
it — the other three answered `items` on UPDATE with a blue wish, so a line could be
added or corrected on an order and nowhere else. The reconcile now lives on the
facade base behind `reconciles_line_items`; these pin that all four share it and hit
their OWN collection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

# adapter → (handle prefix, upstream collection)
_CASES = [
    (QuoteAdapter, "quo_", "/api/v3/offers"),
    (SalesOrderAdapter, "so_", "/api/v3/salesOrders"),
    (SalesInvoiceAdapter, "si_", "/api/v3/invoices"),
    (CreditNoteAdapter, "cn_", "/api/v3/creditNotes"),
]


def _adapter(cls: type, calls: list, currency: str = "EUR"):
    a = cls()

    async def fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        return 200, {
            "data": {
                "id": 7,
                "financials": {"currency": currency},
                "lineItems": [
                    {"id": 111, "order": 1, "product": {"id": "1"}, "quantity": 1, "unit": "piece"},
                    {"id": 222, "order": 2, "product": {"id": "2"}, "quantity": 1, "unit": "piece"},
                ],
            }
        }

    async def fake_li_call(method, url, token, al, client, payload=None):  # noqa: ANN001, ANN202
        calls.append((method, url, payload))
        return {"POST": 201, "PATCH": 200, "DELETE": 204}.get(method, 200), {"data": {"id": "999"}}

    a._get = fake_get  # type: ignore[method-assign]
    a._li_call = fake_li_call  # type: ignore[method-assign]
    return a


def _patch(a, handle: str, items: list[dict[str, Any]]):
    return asyncio.run(
        a.request(
            method="PATCH",
            handle=handle,
            query=[],
            body=json.dumps({"items": items}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )


def test_all_four_opt_into_the_shared_reconcile():
    for cls, _prefix, _path in _CASES:
        assert cls.reconciles_line_items is True, cls.__name__


def test_each_reconciles_against_its_own_collection():
    """A shared implementation must not send every document to /salesOrders."""
    for cls, prefix, path in _CASES:
        calls: list = []
        resp = _patch(
            _adapter(cls, calls),
            f"{prefix}7",
            [
                # keep 111 with a change, drop 222, add a new line
                {"id": "111", "quantity": {"value": 5}},
                {"product": {"id": "prd_3"}, "quantity": {"value": 2}},
            ],
        )
        assert resp.status_code == 200, (cls.__name__, resp.body)
        ops = [(m, u) for m, u, _ in calls]
        assert ("PATCH", f"https://x.test{path}/7/lineItems/111") in ops, cls.__name__
        assert ("DELETE", f"https://x.test{path}/7/lineItems/222") in ops, cls.__name__
        assert ("POST", f"https://x.test{path}/7/lineItems") in ops, cls.__name__


def test_purchase_price_is_patchable_on_every_document_now():
    for cls, prefix, _path in _CASES:
        calls: list = []
        _patch(
            _adapter(cls, calls),
            f"{prefix}7",
            [{"id": "111", "purchasePrice": {"amount": "9.99", "currency": "EUR"}}, {"id": "222"}],
        )
        body = next(b for m, _u, b in calls if m == "PATCH")
        assert body["purchasePrice"] == {"net": {"amount": "9.99", "currency": "EUR"}}, cls.__name__


def test_document_currency_reaches_the_line_on_every_document():
    for cls, prefix, _path in _CASES:
        calls: list = []
        _patch(
            _adapter(cls, calls, currency="USD"),
            f"{prefix}7",
            [{"id": "111", "purchasePrice": {"amount": "9.99"}}, {"id": "222"}],
        )
        body = next(b for m, _u, b in calls if m == "PATCH")
        assert body["purchasePrice"]["net"]["currency"] == "USD", cls.__name__


def test_id_only_item_is_left_untouched():
    for cls, prefix, _path in _CASES:
        calls: list = []
        _patch(_adapter(cls, calls), f"{prefix}7", [{"id": "111"}, {"id": "222"}])
        assert [m for m, _u, _b in calls] == [], cls.__name__


def test_unknown_item_key_is_refused_before_any_upstream_call():
    for cls, prefix, _path in _CASES:
        calls: list = []
        resp = _patch(_adapter(cls, calls), f"{prefix}7", [{"id": "111", "serialNumbers": ["S"]}])
        assert resp.status_code == 409, cls.__name__
        assert "items.serialNumbers" in json.loads(resp.content)["fields"], cls.__name__
        assert calls == [], cls.__name__


def test_create_still_sends_items_inline_not_through_the_sub_resource():
    for cls, _prefix, _path in _CASES:
        body, rejected = cls().map_write(
            {"items": [{"product": {"id": "prd_1"}, "quantity": {"value": 1}}]}, creating=True
        )
        assert "lineItems" in body, cls.__name__
        assert "items" not in rejected, cls.__name__
