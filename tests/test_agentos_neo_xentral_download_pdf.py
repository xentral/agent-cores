"""`downloadPdf` fetches the real document instead of declaring a gap.

The action was a wish on all seven document adapters, with the reason "No public
PDF render endpoint". That was wrong: upstream renders the document on the record
itself via content negotiation — `GET {v3_path}/{id}` with `Accept: application/pdf`
(documented in `documents.yml`, scope `<document>:read`). Measured against mvp on
2026-08-01: offers, salesOrders, invoices, creditNotes, deliveryNotes,
purchaseOrders and returnOrders all answer `200 %PDF-1.3`.

Two constraints shape the return value:

* The gateway parses an action's body as JSON and falls back to
  `bytes.decode(errors="replace")` — raw PDF bytes would reach the caller as
  replacement characters. So the file leaves as base64 inside JSON.
* A rendered document can be large and the caller's context window is not. Beyond
  the cap the action refuses with a readable reason rather than emitting a
  multi-megabyte string.

One upstream behaviour worth remembering: the endpoint serves the ARCHIVED copy
when one exists (written on send and on write protection, not on release) and
renders fresh otherwise — two calls can legitimately differ after a send.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

_DOCUMENTS = [
    QuoteAdapter,
    SalesOrderAdapter,
    SalesInvoiceAdapter,
    CreditNoteAdapter,
    DeliveryNoteAdapter,
    PurchaseOrderAdapter,
    ReturnAdapter,
]
_PDF = b"%PDF-1.3\n... rendered document ..."


def _transport(pdf: bytes = _PDF, status: int = 200, doc_number: str | None = "AB-1") -> Any:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("Accept") == "application/pdf":
            return httpx.Response(status, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(200, json={"data": {"id": "22911", "documentNumber": doc_number}})

    return httpx.MockTransport(handle), seen


def _run(adapter_cls: type, handle: str = "so_22911", **kw: Any) -> tuple[int, dict[str, Any]]:
    transport, seen = _transport(**kw)
    client = httpx.AsyncClient(transport=transport)
    adapter = adapter_cls()

    async def go() -> Any:
        try:
            return await adapter.action(
                action_key="downloadPdf",
                handle=handle,
                body=json.dumps({"ids": [handle], "command": {}}).encode(),
                base_url="https://x.test",
                token="t",
                accept_language=None,
                client=client,
            )
        finally:
            await client.aclose()

    resp = asyncio.run(go())
    _run.requests = seen  # type: ignore[attr-defined]
    return resp.status_code, json.loads(resp.content or b"{}")


# ---- declaration ---------------------------------------------------------


@pytest.mark.parametrize("cls", _DOCUMENTS, ids=lambda c: c.__name__)
def test_every_document_declares_it_and_none_calls_it_a_wish(cls: type) -> None:
    entry = next(a for a in cls().actions() if a["key"] == "downloadPdf")
    assert cls.renders_pdf is True
    assert "wish" not in entry, "declared executable but still described as impossible"
    assert entry.get("description")


def test_an_entity_without_a_printable_form_does_not_get_the_action() -> None:
    assert CustomerAdapter.renders_pdf is False
    assert not any(a["key"] == "downloadPdf" for a in CustomerAdapter().actions())


# ---- the happy path ------------------------------------------------------


@pytest.mark.parametrize("cls", _DOCUMENTS, ids=lambda c: c.__name__)
def test_it_returns_the_pdf_as_a_file_payload(cls: type) -> None:
    status, body = _run(cls)
    assert status == 200
    file = body["result"]["file"]
    assert base64.b64decode(file["contentBase64"]) == _PDF
    assert file["contentType"] == "application/pdf"
    assert file["sizeBytes"] == len(_PDF)


def test_it_asks_the_record_itself_with_the_pdf_accept_header() -> None:
    """No separate endpoint and no files sub-resource — content negotiation on the
    document. Getting this wrong yields the JSON record with a .pdf name."""
    _run(QuoteAdapter, handle="quo_6")
    pdf_call = next(r for r in _run.requests if r.headers.get("Accept") == "application/pdf")
    assert str(pdf_call.url) == "https://x.test/api/v3/offers/6"


def test_the_filename_carries_the_document_number_and_the_entity() -> None:
    """A quote and a purchase order on mvp both answer to 100000 — two files named
    100000.pdf in one store are one file."""
    _, body = _run(QuoteAdapter, doc_number="100000")
    assert body["result"]["file"]["filename"] == "Quote-100000.pdf"


def test_a_document_without_a_number_falls_back_to_its_id() -> None:
    _, body = _run(QuoteAdapter, handle="quo_6", doc_number=None)
    assert body["result"]["file"]["filename"] == "Quote-6.pdf"


# ---- refusals ------------------------------------------------------------


def test_without_an_id_there_is_nothing_to_render() -> None:
    adapter = QuoteAdapter()
    resp = asyncio.run(
        adapter.action(
            action_key="downloadPdf",
            handle=None,
            body=json.dumps({"ids": [], "command": {}}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )
    assert resp.status_code == 422


def test_a_response_that_is_not_a_pdf_is_refused_not_forwarded() -> None:
    """Content negotiation that quietly fell back to JSON would otherwise be handed
    on as a 'PDF' the caller cannot open."""
    status, body = _run(QuoteAdapter, pdf=b'{"data": {"id": "6"}}')
    assert status == 502
    assert "did not answer with a PDF" in body["title"]


def test_an_upstream_error_is_forwarded_verbatim() -> None:
    status, _ = _run(QuoteAdapter, pdf=b'{"title": "no access"}', status=403)
    assert status == 403


def test_an_oversized_document_is_refused_with_its_size() -> None:
    big = b"%PDF-1.3" + b"x" * (QuoteAdapter._PDF_MAX_BYTES + 1)
    status, body = _run(QuoteAdapter, pdf=big)
    assert status == 413
    assert str(len(big)) in body["detail"]
