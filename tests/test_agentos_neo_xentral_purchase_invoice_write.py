"""PurchaseInvoice writes against the entity API's line-item diff.

Upstream does not manage `supplierInvoice.lineItems` as a sub-resource like the
v3 documents do. The whole collection travels in the document body, and every
entry must carry an `actionIndicator` of `Create` / `Update` / `Delete` — a field
that is REQUIRED and appears in no schema (`GET /api/metadata/supplierInvoice`
omits it entirely). Outward the collection keeps the contract the other documents
use, so these tests pin the translation between the two.

The second thing pinned here is a guard, not a mapping: upstream accepts an empty
`POST` and books an invoice with no creditor, no date and no position. There are
no required fields at all — `requiredOnCreate` reports only `id`. A facade that
passes that through turns a malformed call into a real accounting document.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_invoice import (
    PurchaseInvoiceAdapter,
)

_RECORD = {
    "id": "50",
    "uuid": "u-inv",
    "currency": "EUR",
    "associatedAddress": {"id": "14"},
    "lineItems": [
        {"uuid": "u-a", "productName": "A", "quantity": "2.0000", "netPrice": "15.0000"},
        {"uuid": "u-b", "productName": "B", "quantity": "1.0000", "netPrice": "40.0000"},
    ],
}


class _Upstream:
    def __init__(self):
        self.writes: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method in ("POST", "PATCH"):
            self.writes.append((request.method, json.loads(request.content or b"{}")))
            return httpx.Response(201 if request.method == "POST" else 200, json={"data": _RECORD})
        if path.endswith("/u-inv"):
            return httpx.Response(200, json={"data": _RECORD})
        if path.endswith("/supplierInvoice"):
            return httpx.Response(200, json={"data": [_RECORD], "meta": {"total": 1}})
        raise AssertionError(f"unexpected {request.method} {path}")


def _write(up: _Upstream, method: str, handle, model: dict):
    a = PurchaseInvoiceAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method=method,
                handle=handle,
                query=[],
                body=json.dumps(model).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def test_create_without_a_supplier_is_refused_by_this_core():
    """Upstream would accept it and book an invoice with no creditor."""
    up = _Upstream()
    resp = _write(up, "POST", None, {"references": {"supplierInvoiceNumber": "X"}})
    assert resp.status_code == 422
    body = json.loads(resp.content)
    assert body["source"] == "core"  # our refusal, not upstream's
    assert not up.writes  # nothing reached upstream


def test_create_sends_the_supplier_as_the_associated_address():
    up = _Upstream()
    _write(up, "POST", None, {"supplier": {"id": "sup_14"}})
    assert up.writes[0][1]["associatedAddress"] == {"id": "14"}


def test_new_lines_are_marked_create_and_carry_a_net_price():
    """`netPrice` is the one required attribute of a new line — a Create without
    it is a 400, so a caller who only names a line must not get a failed write."""
    up = _Upstream()
    _write(
        up,
        "POST",
        None,
        {"supplier": {"id": "sup_14"}, "items": [{"name": "Beratung"}]},
    )
    line = up.writes[0][1]["lineItems"][0]
    assert line["actionIndicator"] == "Create"
    assert line["productName"] == "Beratung"
    assert line["netPrice"] == "0"
    assert "uuid" not in line


def test_an_item_with_an_id_updates_that_line_by_uuid():
    up = _Upstream()
    _write(up, "PATCH", "pi_u-inv", {"items": [{"id": "pii_u-a"}, {"id": "pii_u-b"}]})
    lines = {x.get("uuid"): x for x in up.writes[0][1]["lineItems"]}
    assert lines["u-a"]["actionIndicator"] == "Update"
    assert lines["u-b"]["actionIndicator"] == "Update"


def test_an_omitted_line_is_deleted():
    """Collection replace — the same contract the sub-resource documents use."""
    up = _Upstream()
    _write(up, "PATCH", "pi_u-inv", {"items": [{"id": "pii_u-a", "name": "A"}]})
    lines = up.writes[0][1]["lineItems"]
    assert {x["actionIndicator"] for x in lines} == {"Update", "Delete"}
    assert next(x for x in lines if x["actionIndicator"] == "Delete")["uuid"] == "u-b"


def test_a_caller_cannot_forge_the_removal_list():
    """`__removedItems` is part of the path the request took, not caller input."""
    up = _Upstream()
    _write(
        up,
        "PATCH",
        "pi_u-inv",
        {"items": [{"id": "pii_u-a"}, {"id": "pii_u-b"}], "__removedItems": ["u-a"]},
    )
    lines = up.writes[0][1]["lineItems"]
    assert not [x for x in lines if x["actionIndicator"] == "Delete"]


def test_a_product_link_wins_over_a_line_name():
    """Upstream fills productName from the product, so sending both is a lie."""
    up = _Upstream()
    _write(
        up,
        "POST",
        None,
        {
            "supplier": {"id": "sup_14"},
            "items": [{"product": {"id": "prd_61999"}, "name": "ignoriert"}],
        },
    )
    line = up.writes[0][1]["lineItems"][0]
    assert line["product"] == {"id": "61999"}
    assert "productName" not in line


def test_fields_that_answer_2xx_without_persisting_are_refused():
    """Measured on mvp: `costCenterValue` and `documentNumber` accept and drop."""
    up = _Upstream()
    resp = _write(up, "PATCH", "pi_u-inv", {"costCenter": "KST-9"})
    assert resp.status_code == 409
    assert not up.writes


def test_a_created_record_is_read_back_by_uuid_not_by_id():
    """`GET /api/entity/supplierInvoice/50` answers "not found with uuid 50", and
    `id` is not filterable — reading a new record back by its id cannot work."""
    up = _Upstream()
    resp = _write(up, "POST", None, {"supplier": {"id": "sup_14"}})
    assert resp.status_code == 201
    assert json.loads(resp.content)["data"]["id"] == "pi_u-inv"


def test_a_free_text_line_keeps_its_name_on_read():
    """A purchase-invoice line often has no product; the name must not ride on
    the product reference, which collapses to null there."""
    d = PurchaseInvoiceAdapter().map_read(_RECORD)
    assert [i["name"] for i in d["items"]] == ["A", "B"]
    assert d["items"][0]["product"] is None
