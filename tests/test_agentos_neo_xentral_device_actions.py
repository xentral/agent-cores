"""Printer.printDocument + EmailAccount.sendEmail: payload building, guards.

Both actions hit real-world surfaces (paper, inboxes) — the tests pin the
upstream payload shapes, the fail-closed input validation, and the
correspondence logging fan-in on sends.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.email_account import (
    EmailAccountAdapter,
)
from xentral_entity_cores.agentos_neo_xentral.emulated.printer import PrinterAdapter

BASE = "https://tenant.example"


class Upstream:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict]]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"title": f"no route {key}"})
        status, payload = self.routes[key]
        return httpx.Response(status, json=payload)


def _action(adapter, up: Upstream, action_key: str, ids, command):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await adapter.action(
                action_key=action_key,
                handle=None,
                body=json.dumps({"ids": ids, "command": command}).encode(),
                base_url=BASE,
                token="t",
                client=client,
            )

    return asyncio.run(go())


_PDF_B64 = base64.b64encode(b"%PDF-1.4 tiny").decode()


def test_print_document_builds_print_job():
    up = Upstream({("POST", "/api/v1/printJobs"): (201, {"data": {"id": "9"}})})
    resp = _action(
        PrinterAdapter(),
        up,
        "printDocument",
        ["prn_4"],
        {"fileContent": _PDF_B64, "fileName": "label.pdf", "quantity": 2},
    )
    assert resp.status_code == 200
    sent = json.loads(up.requests[0].content)
    assert sent["printer"] == {"id": "4"}  # speaking prefix stripped
    assert sent["file"] == {"type": "pdf", "content": _PDF_B64, "name": "label.pdf"}
    assert sent["quantity"] == 2
    assert json.loads(resp.content)["data"]["printJobId"] == "9"


def test_print_document_requires_file_content():
    up = Upstream({})
    resp = _action(PrinterAdapter(), up, "printDocument", ["prn_4"], {"fileName": "x.pdf"})
    assert resp.status_code == 422
    assert up.requests == []


def test_send_email_sends_and_logs_correspondence():
    up = Upstream(
        {
            ("PATCH", "/api/v3/emailAccounts/25/actions/sendEmail"): (200, {}),
            ("POST", "/api/entity/correspondence"): (201, {"data": {"id": "5", "uuid": "u-5"}}),
        }
    )
    resp = _action(
        EmailAccountAdapter(),
        up,
        "sendEmail",
        ["eml_25"],
        {
            "to": "kunde@example.com",
            "subject": "Ihre Lieferung",
            "body": "<p>Hallo</p>",
            "cc": "a@example.com, b@example.com",
            "customer": "cus_20201",
        },
    )
    assert resp.status_code == 200
    send = json.loads(up.requests[0].content)
    assert send["to"] == "kunde@example.com"
    assert send["addSignature"] is True
    assert send["cc"] == ["a@example.com", "b@example.com"]
    log = json.loads(up.requests[1].content)
    assert log["subject"] == "OUT: Ihre Lieferung"  # outbound marker only in CRM
    assert log["isSent"] is True
    assert log["recipientAddress"] == {"id": "20201"}
    data = json.loads(resp.content)["data"]
    assert data["sent"] is True and data["correspondence"] == "cor_u-5"


def test_send_email_retries_without_add_signature():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/sendEmail"):
            calls["n"] += 1
            body = json.loads(request.content)
            if "addSignature" in body:
                return httpx.Response(400, json={"title": "unknown field addSignature"})
            return httpx.Response(200, json={})
        return httpx.Response(201, json={"data": {"uuid": "u-9"}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await EmailAccountAdapter().action(
                action_key="sendEmail",
                handle="eml_25",
                body=json.dumps(
                    {"command": {"to": "x@example.com", "subject": "s", "body": "b"}}
                ).encode(),
                base_url=BASE,
                token="t",
                client=client,
            )

    resp = asyncio.run(go())
    assert resp.status_code == 200
    assert calls["n"] == 2  # 400 with addSignature → retried without


def test_send_email_rejects_bad_attachments():
    up = Upstream({})
    for bad, why in (
        ([{"fileName": "a.pdf"}], "missing content"),
        ([{"fileName": "a.pdf", "fileContent": "not-base64!!"}], "invalid base64"),
        ([{"fileName": "a.pdf", "fileContent": base64.b64encode(b"HELLO").decode()}], "bad magic"),
    ):
        resp = _action(
            EmailAccountAdapter(),
            up,
            "sendEmail",
            ["eml_25"],
            {"to": "x@example.com", "subject": "s", "body": "b", "attachments": bad},
        )
        assert resp.status_code == 422, why
    assert up.requests == []  # nothing ever sent


def test_send_email_requires_to_subject_body():
    up = Upstream({})
    resp = _action(EmailAccountAdapter(), up, "sendEmail", ["eml_25"], {"to": "x@example.com"})
    assert resp.status_code == 422
    assert up.requests == []
