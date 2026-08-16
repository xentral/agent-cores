"""Booking stock is the one thing on a tenant that cannot be put back.

`Return.restock` and `PurchaseOrder.createGoodsReceipt` both POST to
`/api/v1/{returns|purchaseOrders}/{id}/goodsReceipts`. Probing them is safe by
default — both REQUIRE `date` and `items`, and `goods_receipt_payload` validates
before the upstream is ever called, so an empty command books nothing. Earning a
`pass` is what costs: goods receipts have no delete and no read (`GET` answers 501),
stock movements are append-only, and cancelling a receipt exists only in the legacy
UI.

So the probe books ONE unit against a document it created itself, reads the effect off
the source line — `receivedQuantity` on a return, `fulfillment.received` on a purchase
order — and deletes the document afterwards. Measured on mvp: the document never takes
a number, so it stays deletable even after the booking; the booking stays.

Nothing here talks to a tenant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.checks import verify
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN

FIXTURE = {"warehouse": "wh_9", "storageLocation": "loc_3"}
BODY = {"note": "verify goods-receipt probe"}


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()


class _Order:
    """A purchase order whose line carries a booked counter, the way upstream does:
    `fulfillment.received` starts null and counts up as receipts arrive."""

    manifest = type(
        "M", (), {"key": "PurchaseOrder", "operations": ("list", "read", "create", "delete")}
    )()

    def __init__(self, *, deaf: bool = False, refuse: int | None = None, lines: bool = True):
        self.deaf = deaf
        self.refuse = refuse
        self.lines = lines
        self.received: Any = None
        self.calls: list[str] = []
        self.booked: list[dict[str, Any]] = []

    def _record(self) -> dict[str, Any]:
        items = (
            [
                {
                    "id": "159",
                    "product": {"id": "prd_62007"},
                    "fulfillment": {"received": self.received, "invoiced": None},
                }
            ]
            if self.lines
            else []
        )
        return {"id": "po_1", "status": "draft", "number": None, "items": items}

    async def request(self, *, method, handle, query, body, base_url, token, **_):
        if method == "POST":
            self.calls.append("POST")
            return _Resp(201, {"data": self._record()})
        if method == "DELETE":
            self.calls.append("DELETE")
            return _Resp(204)
        return _Resp(200, {"data": self._record()})

    async def action(self, *, action_key: str, body: bytes, **_: Any) -> _Resp:
        self.calls.append(action_key)
        if self.refuse:
            return _Resp(self.refuse, {"title": "nope"})
        self.booked.append(json.loads(body)["command"])
        if not self.deaf:
            self.received = (self.received or 0) + 1
        return _Resp(201, {"data": {"object": "goodsReceipt", "id": "gr_uuid-1"}})


def _probe(order: _Order) -> dict[str, tuple[str, str]]:
    return asyncio.run(
        verify._probe_goods_receipt(
            order, BODY, "supplier", ["sup_1"], "https://x", "t", fixture=FIXTURE
        )
    )


# ---- the effect --------------------------------------------------------


def test_the_counter_on_the_booked_line_is_the_proof() -> None:
    order = _Order()
    out = _probe(order)
    verdict, note = out["createGoodsReceipt"]
    assert verdict == PROVEN
    assert "0 → 1" in note.replace("0.0", "0").replace("1.0", "1")
    assert "loc_3" in note and "prd_62007" in note, "the note must say what went where"
    assert "gr_uuid-1" in note


def test_it_books_exactly_one_unit_onto_the_fixture_location() -> None:
    order = _Order()
    _probe(order)
    (command,) = order.booked
    assert command["items"][0]["quantity"] == 1
    (putaway,) = command["items"][0]["putaways"]
    assert putaway == {"quantity": 1, "warehouse": "wh_9", "storageLocation": "loc_3"}
    assert command["items"][0]["orderItem"] == "159", "the booking must name the line it hits"
    assert command["date"], "upstream rejects a receipt without a posting date"


def test_a_2xx_that_moves_no_counter_is_a_failure() -> None:
    """The whole reason the verdict is read off the document. An action that answers
    201 and leaves the line at zero is what `executed` used to paint green."""
    order = _Order(deaf=True)
    verdict, note = _probe(order)["createGoodsReceipt"]
    assert verdict == "fail"
    assert "accepted without effect" in note


def test_a_refused_booking_is_a_failure_and_still_cleans_up() -> None:
    order = _Order(refuse=422)
    verdict, _ = _probe(order)["createGoodsReceipt"]
    assert verdict == "fail"
    assert "DELETE" in order.calls, "the document we made must not be left behind"


# ---- what it must not do ------------------------------------------------


def test_a_document_without_a_line_is_never_booked_against() -> None:
    """No line means no `orderItem` and nothing to read a counter off. Booking anyway
    would move stock for an assertion that cannot be made."""
    order = _Order(lines=False)
    assert _probe(order) == {}
    assert "createGoodsReceipt" not in order.calls
    assert order.calls == ["POST", "DELETE"]


def test_the_document_is_always_deleted_again() -> None:
    """It never takes a number, so it stays deletable even after the booking — the
    residue should be the stock movement alone, not a stray order as well."""
    order = _Order()
    _probe(order)
    assert order.calls == ["POST", "createGoodsReceipt", "DELETE"]


def test_a_missing_counter_reads_as_zero_not_as_a_crash() -> None:
    """Upstream leaves it null until the first receipt; `None < 1` would blow up the
    comparison the probe exists to make."""
    assert verify._counter_at({}, "fulfillment.received") == 0.0
    assert verify._counter_at({"fulfillment": {"received": None}}, "fulfillment.received") == 0.0
    assert verify._counter_at({"receivedQuantity": "3"}, "receivedQuantity") == 3.0
