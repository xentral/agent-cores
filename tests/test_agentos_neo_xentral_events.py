"""agentos_neo_xentral — the push half (``events.py``), over a mocked transport.

The source moved here from the backend. These tests pin what a relocation can
break silently:

* the **source id**, which every stored subscription names — change it and the
  platform can no longer find the source that verifies a live delivery, nor the
  one that can tear it down;
* the request shapes and the ``Location``-header id extraction, both measured
  against a live tenant;
* the signature recipe, which is the trust boundary for every inbound event;
* that the source reads its credentials off the context and never resolves —
  it *cannot* resolve, the chain is a backend internal.

Run from the repo root::

    PYTHONPATH=<agent-os>/backend uv run pytest tests/test_agentos_neo_xentral_events.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from entity_registry.core_sdk import (
    EventSignatureError,
    EventSourceContext,
    EventSourceError,
)
from xentral_entity_cores.agentos_neo_xentral.events import (
    XENTRAL_EVENT_SOURCE_ID,
    XentralEventSource,
)
from xentral_entity_cores.agentos_neo_xentral.manifest import CORE

BASE = "https://tenant.xentral.biz"
TOKEN = "tok"
# Xentral rejects a signature key under 20 characters, so the fixture has to be
# at least that long while staying obviously fake.
SECRET = "not-a-real-signature-key-for-tests"
TIMESTAMP = "1785735419"

ENVELOPE = {"type": "com.xentral.salesOrder.created.v1", "body": {"salesOrderId": 22957}}
BODY = json.dumps(ENVELOPE).encode()


def _ctx(**over) -> EventSourceContext:
    """A context as the platform hands it over — credentials already resolved."""
    kw = {
        "license_id": "lic",
        "core_id": "agentos_neo_xentral",
        "base_url": BASE,
        "token": TOKEN,
    }
    kw.update(over)
    return EventSourceContext(**kw)


def _sign(body: bytes = BODY, secret: str = SECRET, timestamp: str = TIMESTAMP) -> dict[str, str]:
    """Xentral's documented recipe: hmac over body bytes + the ASCII timestamp."""
    message = body + timestamp.encode("ascii")
    return {
        "xentral-signature": hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest(),
        "xentral-request-timestamp": timestamp,
    }


def _transport(monkeypatch, handler):
    """Answer every outgoing request with ``handler(request)``, recording it."""
    seen: list[httpx.Request] = []

    async def _send(self, request, **kw):
        seen.append(request)
        response = handler(request)
        response.request = request
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", _send)
    return seen


# --------------------------------------------------------------------------- #
# Identity and declaration
# --------------------------------------------------------------------------- #


def test_the_source_id_is_the_one_stored_subscriptions_name():
    """It travelled with the code and must not change. A stored subscription
    records this string; a renamed source cannot verify a live delivery and
    cannot be torn down either."""
    assert XENTRAL_EVENT_SOURCE_ID == "xentral_webhooks"
    assert XentralEventSource.id == "xentral_webhooks"


def test_the_core_declares_it():
    """Without this the platform finds no source for the core, and every
    ERP-event trigger silently becomes unavailable."""
    assert isinstance(CORE.event_source, XentralEventSource)


def test_it_declares_no_pause_and_no_url_token():
    """Xentral has no suspend, and its signature IS the auth layer — a URL
    token as well would change every live subscription's URL for no gain."""
    assert XentralEventSource.supports_pause is False
    assert XentralEventSource.delivery_token_required is False
    with pytest.raises(NotImplementedError):
        asyncio.run(XentralEventSource().set_enabled(_ctx(), subscription_id="1", enabled=False))


# --------------------------------------------------------------------------- #
# Credentials come from the platform
# --------------------------------------------------------------------------- #


def test_a_context_without_credentials_is_refused():
    """The source cannot resolve them — the chain is a backend internal — so an
    empty pair must fail here rather than go out as an anonymous request."""
    with pytest.raises(EventSourceError):
        asyncio.run(XentralEventSource().list_event_types(_ctx(base_url="", token="")))


def test_the_source_has_no_resolver_of_its_own():
    """If it grew one it would have to import the backend's credential chain,
    which is exactly what a vendored core cannot do."""
    assert getattr(XentralEventSource, "resolve_auth", None) is None


def test_the_request_carries_the_context_pair(monkeypatch):
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    asyncio.run(XentralEventSource().list_event_types(_ctx()))
    assert str(seen[0].url).startswith(f"{BASE}/api/v1/webhookEventTypes")
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


def test_the_catalogue_asks_for_the_schema_and_passes_it_verbatim(monkeypatch):
    """``?include=schema`` (API-835) is safe to send unconditionally: an
    installation that does not know it answers the plain list. The schema is
    handed on unreshaped — a rewritten one would describe our idea of the
    delivery rather than Xentral's."""
    schema = {
        "type": "object",
        "properties": {"type": {"type": "string"}, "body": {"properties": {"salesOrderId": {}}}},
    }
    rows = [
        {"id": "com.xentral.salesOrder.imported.v1", "group": "salesOrder", "schema": schema},
        {"id": "com.xentral.user.changedPage.v1", "group": "user", "schema": None},
    ]
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": rows}))

    types = asyncio.run(XentralEventSource().list_event_types(_ctx()))

    assert seen[0].url.params["include"] == "schema"
    by_id = {t.id: t for t in types}
    assert by_id["com.xentral.salesOrder.imported.v1"].schema == schema
    # `schema: null` is "not published", NOT "this event has no fields".
    assert by_id["com.xentral.user.changedPage.v1"].schema is None


def test_a_catalogue_error_is_reported_not_swallowed(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(403, text="forbidden"))
    with pytest.raises(EventSourceError) as exc:
        asyncio.run(XentralEventSource().list_event_types(_ctx()))
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# Subscribe / unsubscribe
# --------------------------------------------------------------------------- #


def test_subscribe_sends_the_measured_shape_and_reads_the_location_id(monkeypatch):
    """MEASURED: 201 with an EMPTY body, the new id only in ``Location``."""
    seen = _transport(
        monkeypatch,
        lambda r: httpx.Response(201, headers={"Location": "/api/v1/webhooks/17"}),
    )

    sub = asyncio.run(
        XentralEventSource().subscribe(
            _ctx(),
            event_id="com.xentral.salesOrder.created.v1",
            delivery_url="https://agent.example.com/wh-erp/shr_1",
            label="Order watcher",
        )
    )

    body = json.loads(seen[0].content)
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{BASE}/api/v1/webhooks"
    assert body["url"] == "https://agent.example.com/wh-erp/shr_1"
    assert body["events"] == [{"id": "com.xentral.salesOrder.created.v1"}]
    # Xentral's schema rejects a signature key under 20 characters.
    assert len(body["signatureKey"]) >= 20
    assert sub.subscription_id == "17"
    assert sub.secret == body["signatureKey"]


def test_subscribe_also_accepts_an_id_in_the_body(monkeypatch):
    """Older versions and plugins answer with a JSON body instead."""
    _transport(monkeypatch, lambda r: httpx.Response(201, json={"data": {"id": "42"}}))
    sub = asyncio.run(
        XentralEventSource().subscribe(
            _ctx(), event_id="e", delivery_url="https://a.test/wh", label=""
        )
    )
    assert sub.subscription_id == "42"


def test_subscribe_without_an_id_raises_rather_than_inventing_one(monkeypatch):
    """Invariant I2: a subscription whose id we do not hold can never be torn
    down. It would deliver forever with nothing able to stop it."""
    _transport(monkeypatch, lambda r: httpx.Response(201))
    with pytest.raises(EventSourceError):
        asyncio.run(
            XentralEventSource().subscribe(
                _ctx(), event_id="e", delivery_url="https://a.test/wh", label=""
            )
        )


def test_a_rejected_subscribe_carries_the_upstream_status(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(422, text="unknown event id"))
    with pytest.raises(EventSourceError) as exc:
        asyncio.run(
            XentralEventSource().subscribe(
                _ctx(), event_id="nope", delivery_url="https://a.test/wh", label=""
            )
        )
    assert exc.value.status_code == 422


def test_unsubscribe_tolerates_a_webhook_that_is_already_gone(monkeypatch):
    """The goal state is "not subscribed". Failing on a 404 would strand our own
    record for a webhook someone removed by hand in Xentral."""
    _transport(monkeypatch, lambda r: httpx.Response(404))
    asyncio.run(XentralEventSource().unsubscribe(_ctx(), subscription_id="17"))


def test_unsubscribe_surfaces_a_real_failure(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(EventSourceError):
        asyncio.run(XentralEventSource().unsubscribe(_ctx(), subscription_id="17"))


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #


def test_pruning_removes_only_what_points_at_our_url(monkeypatch):
    """Two concurrent activations both register and only the second id is
    persisted, so the first would deliver forever with no handle to remove it.
    Converging on the URL fixes that — but an unrelated subscription of the
    tenant's must survive."""
    url = "https://agent.example.com/wh-erp/shr_1"
    rows = [
        {"id": "666", "url": url},
        {"id": "111", "url": "https://somewhere.else/hook"},
    ]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(204)

    seen = _transport(monkeypatch, handler)
    removed = asyncio.run(XentralEventSource().prune_subscriptions_for(_ctx(), delivery_url=url))

    assert removed == 1
    deleted = [str(r.url) for r in seen if r.method == "DELETE"]
    assert deleted == [f"{BASE}/api/v1/webhooks/666"]


def test_pruning_that_cannot_list_gives_up_instead_of_failing(monkeypatch):
    """Best-effort by contract: a tenant whose token cannot list webhooks must
    still be able to subscribe. Failing the activation over a cleanup would be
    worse than the duplicate it prevents."""
    _transport(monkeypatch, lambda r: httpx.Response(403, text="nope"))
    assert (
        asyncio.run(
            XentralEventSource().prune_subscriptions_for(_ctx(), delivery_url="https://a.test/wh")
        )
        == 0
    )


# --------------------------------------------------------------------------- #
# Delivery — the trust boundary
# --------------------------------------------------------------------------- #


def test_a_correctly_signed_delivery_verifies_and_decodes():
    parsed = XentralEventSource().parse_delivery(raw_body=BODY, headers=_sign(), secret=SECRET)
    assert parsed.event_id == "com.xentral.salesOrder.created.v1"
    # Handed to workflows verbatim as ``trigger.body`` — any reshaping here
    # breaks every template already authored against it.
    assert parsed.payload == ENVELOPE


@pytest.mark.parametrize(
    "case",
    ["tampered_body", "tampered_timestamp", "wrong_secret", "no_signature", "no_timestamp"],
)
def test_a_delivery_that_does_not_authenticate_is_rejected(case):
    headers = _sign()
    body = BODY
    secret = SECRET
    if case == "tampered_body":
        body = BODY + b" "
    elif case == "tampered_timestamp":
        headers["xentral-request-timestamp"] = "1785735420"
    elif case == "wrong_secret":
        headers = _sign(secret="other-key-entirely")
    elif case == "no_signature":
        headers.pop("xentral-signature")
    elif case == "no_timestamp":
        headers.pop("xentral-request-timestamp")

    with pytest.raises(EventSignatureError):
        XentralEventSource().parse_delivery(raw_body=body, headers=headers, secret=secret)


def test_an_empty_secret_never_verifies():
    """A subscription whose key was lost must fail closed. Comparing an empty
    key against an empty header would otherwise authenticate anyone."""
    with pytest.raises(EventSignatureError):
        XentralEventSource().parse_delivery(raw_body=BODY, headers=_sign(), secret="")


def test_a_non_json_body_still_verifies_and_falls_back():
    """Authenticity does not depend on the body being JSON — same fallback the
    receiver used before this source existed."""
    raw = b"not json at all"
    parsed = XentralEventSource().parse_delivery(raw_body=raw, headers=_sign(raw), secret=SECRET)
    assert parsed.payload == {"raw": "not json at all"}
    assert parsed.event_id == ""
