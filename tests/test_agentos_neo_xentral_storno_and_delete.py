"""Storno semantics differ by document, and every document can discard a draft.

Two cancellation models exist in Xentral (verified against the real source +
live against mvp):

* Status-storno (SalesOrder/Quote/DeliveryNote/Return/PurchaseOrder): a released
  document is cancelled by a status flip — v3 `PATCH .../actions/cancel`.
* Counter-document storno (Invoice/CreditNote): a released invoice is write-
  protected (GoBD) and CANNOT be status-cancelled; it is reversed by creating a
  cancellation credit note — `POST /api/v1/creditNotes {invoice:{id}}`. A released
  credit note is itself the reversal and can only be cancelled in the legacy UI.

Orthogonally, a DRAFT of any document is discarded with `delete`
(`DELETE /v3/{doc}/{id}`); the upstream 409s on non-drafts, so the op is safe to
expose everywhere.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

_ALL_DOCS = [
    SalesOrderAdapter,
    QuoteAdapter,
    DeliveryNoteAdapter,
    ReturnAdapter,
    PurchaseOrderAdapter,
    SalesInvoiceAdapter,
    CreditNoteAdapter,
]


# --- draft delete is enabled on every document -----------------------------
@pytest.mark.parametrize("adapter_cls", _ALL_DOCS)
def test_delete_is_a_declared_operation(adapter_cls):
    assert "delete" in adapter_cls().manifest.operations


# --- invoice storno = create a cancellation credit note --------------------
def test_invoice_cancel_routes_to_credit_note_create():
    route = SalesInvoiceAdapter().action_map["cancel"]
    assert isinstance(route, dict)  # NOT the legacy ("PATCH","cancel") — that 404s
    assert route["method"] == "POST"
    assert route["path"] == "/api/v1/creditNotes"
    # the invoice id is injected into the body (v1 wants a string of digits)
    assert route["body"]["invoice"]["id"] == "{id}"


def test_invoice_cancel_step_is_destructive_and_explains_storno():
    groups = {g["key"]: g for g in SalesInvoiceAdapter().steps()}
    cmd = {c["key"]: c for c in groups["documentStatus"]["commands"]}["cancel"]
    assert cmd["destructive"] is True
    assert "credit note" in cmd["description"].lower()
    assert "documentNumber" in cmd["command"]["properties"]


def test_invoice_no_longer_advertises_stale_create_credit_note_wish():
    keys = {a["key"] for a in SalesInvoiceAdapter().actions()}
    assert "createCreditNote" not in keys  # fulfilled by `cancel` now


def test_invoice_partially_cancelled_status_is_surfaced_not_draft():
    # A partial storno leaves the invoice partiallyCancelled (v3); it must not
    # fall back to the "draft" default, which would read as un-cancelled.
    a = SalesInvoiceAdapter()
    assert a.map_read({"status": "partiallyCancelled"})["status"] == "partiallyCancelled"
    assert a.map_read({"status": "cancelled"})["status"] == "cancelled"
    assert a.map_read({"status": "released"})["status"] == "open"
    values = {o["value"] for o in a.fields()["status"]["options"]}
    assert {"partiallyCancelled", "cancelled"} <= values


# --- credit note has NO v3 cancel ------------------------------------------
def test_credit_note_has_no_cancel_route():
    assert "cancel" not in CreditNoteAdapter().action_map


def test_credit_note_cancel_step_is_an_honest_wish():
    groups = {g["key"]: g for g in CreditNoteAdapter().steps()}
    cmd = {c["key"]: c for c in groups["documentStatus"]["commands"]}["cancel"]
    assert cmd.get("wish")  # declared but no upstream (legacy-only)


# --- return -> credit note (the return's financial resolution) -------------
def test_return_create_credit_note_routes_to_v1_return_action():
    route = ReturnAdapter().action_map["createCreditNote"]
    assert route["method"] == "POST"
    assert route["path"] == "/api/v1/returns/{id}/actions/createCreditNote"
    # both flags are required upstream — the facade defaults them
    assert route["body"]["isApproved"] is True
    assert route["body"]["isPaid"] is False


def test_return_create_credit_note_is_a_real_action():
    by_key = {a["key"]: a for a in ReturnAdapter().actions()}
    cmd = by_key["createCreditNote"]
    assert not cmd.get("wish")  # no longer a wish — it is wired
    assert cmd["destructive"] is True
    assert {"isApproved", "isPaid"} <= set(cmd["command"]["properties"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
