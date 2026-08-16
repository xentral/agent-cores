"""An invoice storno books a second document, so the probe must own both.

A released invoice is write-protected (GoBD), so its `cancel` is not the status flip
the other documents have — the core POSTs /api/v1/creditNotes {invoice:{id}}, which
creates a cancellation credit note and marks the invoice cancelled. That makes the
generic probe worse here than anywhere else: `cancel` takes no input, so an empty
command commits, and pointed at a SAMPLED invoice it books a cancellation against a
real customer document. It already did — the verdict this replaces was `executed`.

Both halves are checked because either alone is too weak: a status that flips without
a counter-document is a broken storno, and a credit note beside an invoice that is
still open is worse.

Nothing here talks to a tenant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.checks import verify
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()


class _Invoice:
    """Models the real storno: release numbers the invoice, cancel marks it cancelled
    AND returns the credit note it created under `result`."""

    manifest = type(
        "M", (), {"key": "SalesInvoice", "operations": ("list", "read", "create", "delete")}
    )()

    def __init__(self, *, no_credit_note: bool = False, refuse: dict[str, int] | None = None):
        self.no_credit_note = no_credit_note
        self.refuse = refuse or {}
        self.status = "draft"
        self.number = None
        self.calls: list[str] = []

    async def request(self, *, method, handle, query, body, base_url, token, **_):
        if method == "POST":
            self.calls.append("POST")
            return _Resp(201, {"data": {"id": "inv_1", "status": self.status, "number": None}})
        if method == "DELETE":
            self.calls.append("DELETE")
            return _Resp(204)
        return _Resp(200, {"data": {"id": "inv_1", "status": self.status, "number": self.number}})

    async def action(self, *, action_key: str, **_: Any) -> _Resp:
        self.calls.append(action_key)
        if action_key in self.refuse:
            return _Resp(self.refuse[action_key], {"title": "nope"})
        if action_key == "release":
            self.status, self.number = "released", "RE-1001"
            return _Resp(200, {"data": {"id": "inv_1"}})
        self.status = "cancelled"
        if self.no_credit_note:
            return _Resp(200, {"data": {"id": "inv_1"}})
        return _Resp(200, {"result": {"id": "cn_9", "number": "GS-77"}})


BODY = {"note": "verify release probe"}


def _probe(inv: _Invoice, *, linked: str | None = "invoice 2263, status 'draft'") -> dict:
    """`_linked_credit_note` reads the credit note back through the CreditNote
    adapter, which is a tenant call — stubbed here so this file keeps its promise of
    talking to nothing. What it returns is the switch under test: a description means
    the counter-document was found and points at an invoice, None means it was not."""
    original = verify._linked_credit_note

    async def _stub(credit_id, base_url, token):
        return linked if credit_id else None

    verify._linked_credit_note = _stub
    try:
        return asyncio.run(
            verify._probe_invoice_storno(inv, BODY, "customer", ["cus_5"], "https://x", "t")
        )
    finally:
        verify._linked_credit_note = original


def test_both_halves_of_the_storno_are_required() -> None:
    inv = _Invoice()
    out = _probe(inv)
    assert out["release"][0] == PROVEN
    assert "RE-1001" in out["release"][1]
    assert out["cancel"][0] == PROVEN
    assert "cn_9" in out["cancel"][1], "the counter-document is half the claim"
    assert "status 'draft'" in out["cancel"][1], "the note carries what upstream made"
    assert inv.calls == ["POST", "release", "cancel"]


def test_a_cancelled_invoice_without_a_credit_note_is_a_broken_storno() -> None:
    """The invoice is closed and the counter-document cannot be found. That is worth
    a red cell — but the note must say exactly that and no more.

    It must not claim the invoice was left released (it was not), and it must not
    claim no credit note was created: upstream answers 201 with an EMPTY body and the
    new id only in the Location header, so the counter-document can exist and be
    invisible from here. Both wordings send a reader somewhere false."""
    inv = _Invoice(no_credit_note=True)
    out = _probe(inv, linked=None)
    assert out["cancel"][0] == "fail"
    assert "no storno credit note could be identified" in out["cancel"][1]
    assert "LEFT RELEASED" not in out["cancel"][1]
    assert "not created" not in out["cancel"][1], (
        "upstream answers 201 with an empty body and the id only in Location — the "
        "credit note may exist and be invisible here, and claiming otherwise cost a "
        "whole investigation once"
    )


def test_an_invoice_that_stays_open_is_reported_as_left_released() -> None:
    """The other failure, and a different repair: cancel did not take at all."""
    inv = _Invoice(refuse={"cancel": 500})
    out = _probe(inv)
    assert out["cancel"][0] == "fail"
    assert "IS LEFT RELEASED" in out["cancel"][1]


def test_a_refused_release_discards_the_invoice_it_made() -> None:
    """It took no number, so it never entered the books and is still deletable."""
    inv = _Invoice(refuse={"release": 400})
    out = _probe(inv)
    assert out["release"][0] == "fail"
    assert "cancel" not in out
    assert inv.calls == ["POST", "release", "DELETE"]


def test_the_probe_never_touches_a_sampled_invoice() -> None:
    """The whole point. Every request this probe makes goes to the record it created,
    so a real customer invoice can never be the one that gets cancelled."""
    inv = _Invoice()
    _probe(inv)
    assert inv.calls.count("POST") == 1, "one invoice, its own"
