"""A document number is never accepted — and the refusal is visible.

Product decision: a Belegnummer always comes from the configured number range. The
core used to carry `number` in each document's `_IGNORE`, so a caller supplying one
got a 201 and a different number, with nothing to see. That is the same silent drop
that cost a CRM migration its foreign keys on Customer; on documents the answer is
the opposite — the value must not be applied — but it still has to be *reported*.

Upstream would take a `documentNumber` on salesOrder, invoice and creditNote
(verified on mvp: stored on create, survives release). Declining it is therefore a
policy choice, not a missing capability, and priorities.json says so per entity.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

# adapter → entity key in priorities.json
_DOCS = [
    (QuoteAdapter, "Quote"),
    (SalesOrderAdapter, "SalesOrder"),
    (SalesInvoiceAdapter, "SalesInvoice"),
    (CreditNoteAdapter, "CreditNote"),
    (PurchaseOrderAdapter, "PurchaseOrder"),
    (DeliveryNoteAdapter, "DeliveryNote"),
    (ReturnAdapter, "Return"),
]

# The three where upstream would actually store a supplied number, so the refusal is
# ours and has to be recorded as a decision rather than an upstream gap.
_UPSTREAM_WOULD_TAKE_IT = {"SalesOrder", "SalesInvoice", "CreditNote"}


def _priorities() -> dict[str, Any]:
    path = pathlib.Path(__file__).parent.parent / "cores/agentos_neo_xentral/priorities.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_number_is_refused_on_create_not_dropped():
    for cls, name in _DOCS:
        body, rejected = cls().map_write({"number": "ALT-70001"}, creating=True)
        assert "number" in rejected, f"{name}: must be reported, not swallowed"
        assert "number" not in body and "documentNumber" not in body, name


def test_number_is_refused_on_update_too():
    for cls, name in _DOCS:
        assert "number" in cls().map_write({"number": "ALT-1"}, creating=False)[1], name


def test_number_left_the_ignore_set_everywhere():
    """`_IGNORE` is what makes a key vanish quietly — `number` must not be in it."""
    for cls, name in _DOCS:
        assert "number" not in cls._IGNORE, name


def test_number_stays_read_only_in_the_schema():
    for cls, name in _DOCS:
        prop = cls().fields()["number"]
        assert prop.get("access") == "readOnly", name
        assert not prop.get("creatable") and not prop.get("updatable"), name


def test_the_decision_is_on_record_for_every_document():
    prio = _priorities()["entities"]
    for _cls, name in _DOCS:
        wishes = [w for w in prio.get(name, []) if w["field"] == "number"]
        assert wishes, f"{name}: the refusal has to be visible, not just implemented"
        assert wishes[0]["ops"] == ["create"], name


def test_the_reason_distinguishes_policy_from_an_upstream_gap():
    """Whoever reads the Steckbrief must be able to tell 'Xentral cannot' from 'we
    decided not to' — otherwise someone re-opens this the next time they read the spec."""
    prio = _priorities()["entities"]
    for _cls, name in _DOCS:
        reason = next(w["reason"] for w in prio[name] if w["field"] == "number")
        if name in _UPSTREAM_WOULD_TAKE_IT:
            assert "WOULD take" in reason, name
            assert "product decision" in reason, name
        else:
            assert "does not declare" in reason, name


def test_a_normal_create_is_unaffected():
    """Refusing `number` must not make ordinary creates noisy."""
    body, rejected = SalesOrderAdapter().map_write({"customer": "cus_3"}, creating=True)
    assert rejected == set()
    assert body


def test_the_409_says_why_instead_of_claiming_upstream_cannot():
    """The refusal reaches the caller as a response body, not as a docs entry. It
    used to assert "read-only upstream today" for every field — false here, since v3
    would store a supplied number. The reason has to travel with the 409."""
    import asyncio

    resp = asyncio.run(
        SalesOrderAdapter().request(
            method="POST",
            handle=None,
            query=[],
            body=json.dumps({"number": "ALT-70003", "customer": "cus_3"}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )
    assert resp.status_code == 409
    payload = json.loads(resp.content)
    assert payload["fields"] == ["number"]
    reason = payload["reasons"]["number"]
    assert "WOULD take" in reason and "product decision" in reason
    # and it must not claim the upstream is incapable
    assert "read-only upstream today" not in payload["detail"]
