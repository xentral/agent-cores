"""Xentral's webhook service as an ``EventSource`` — this core's push half.

A workflow's ``trigger-erp-event`` node (and an agent slot with cadence
``event``) means "run me when something happens in the ERP". *Which* ERP is
whatever core the tenant is on, so the vendor half of that — the subscription
API, the event-id grammar, the signature scheme, the envelope shape — belongs
to the core rather than to the platform. This file is that half for Xentral.

It came from the backend (``entity_registry/cores/events/xentral.py`` plus the
four calls of ``thirdpartytools/xentral/webhooks_client.py``) and moved
unchanged: same source id, same request shapes, same signature recipe, so a
tenant with a live subscription sees no difference. What stayed behind is the
part a core has no business owning — the public share store, the externally
reachable delivery URL, the rollback on a failed activation — and the one
thing it *cannot* own: resolving the tenant's Xentral credentials, which runs
through the platform's Auth0/instance-manager chain. Those arrive on
``ctx.base_url`` / ``ctx.token``, exactly as an entity adapter receives them.

The vendor facts, all verified live against a tenant with the backend's
``tools/erp_event_probe.py``:

* Event ids are dot-segmented with a lowercase-first entity segment —
  ``com.xentral.salesOrder.created.v1``. A tenant serves ~162 of them.
* ``GET /api/v1/webhookEventTypes`` answers ``{id, group}`` per event, and with
  ``?include=schema`` (API-835) each event's JSON Schema for the whole
  delivery. An installation that does not know the parameter ignores it and
  answers the plain list — so a missing ``schema`` means "not published", never
  "this event has no fields".
* ``POST /api/v1/webhooks`` answers ``201`` with an EMPTY body; the new id is
  only in the ``Location`` header.
* Deliveries are signed ``hex(hmac_sha256(body_bytes + timestamp_ascii, key))``
  and carry ``Xentral-Signature`` + ``Xentral-Request-Timestamp``. Three other
  concatenation orders were tried against a real delivery and do not match.
* One order creation emits several events, seconds apart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any
from collections.abc import Mapping

import httpx

from entity_registry.core_sdk import (
    EventSignatureError,
    EventSource,
    EventSourceContext,
    EventSourceError,
    EventSubscription,
    EventType,
    ParsedEvent,
)

logger = logging.getLogger(__name__)

#: Persisted on every subscription this source creates. Deliberately NOT a core
#: id: three cores (``xentral_api``, ``xentral_business_entities``,
#: ``agentos_neo_xentral``) share this one source, and switching between them
#: must not invalidate a live subscription. It keeps the value it had while the
#: source lived in the backend — every stored subscription names it.
XENTRAL_EVENT_SOURCE_ID = "xentral_webhooks"

#: Xentral's schema requires a signature key of at least 20 characters;
#: ``token_urlsafe(24)`` yields 32, leaving headroom.
_SECRET_BYTES = 24

_SIGNATURE_HEADER = "xentral-signature"
_TIMESTAMP_HEADER = "xentral-request-timestamp"

_TIMEOUT = 15.0


class XentralEventSource:
    """Implements the SDK's ``EventSource`` against ``/api/v1/webhooks``."""

    id = XENTRAL_EVENT_SOURCE_ID
    #: Xentral has no suspend — a subscription exists or it does not.
    supports_pause = False
    #: Every delivery is HMAC-signed, which IS the auth layer here — no token
    #: gate on the public URL (see the platform's ``open_subscription``).
    delivery_token_required = False

    # -- transport -------------------------------------------------------- #

    @staticmethod
    def _auth(ctx: EventSourceContext) -> tuple[str, str]:
        """The credentials the platform put on the context.

        A core cannot resolve these for itself — the chain runs through the
        platform's Auth0/instance-manager cascade and the tenant's pinned ERP
        account — so it reads them instead. Empty means the caller skipped that
        resolution, which raises rather than falling back: there is nothing here
        to fall back TO, and an "anonymous" call would just fail further away
        from its cause.
        """
        if not ctx.base_url or not ctx.token:
            raise EventSourceError(
                "No Xentral credentials on the event context — the platform must "
                "resolve them before calling this source.",
                500,
            )
        return ctx.base_url, ctx.token

    @classmethod
    async def _request(
        cls,
        ctx: EventSourceContext,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One Xentral call. Returns the raw response — the callers below branch
        on status (404 tolerated on delete, 201-with-empty-body on create), so
        raising here would throw away what they need."""
        base, token = cls._auth(ctx)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "xentral-ai-agent",
        }
        url = f"{base.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await client.request(method, url, headers=headers, json=json_body, params=params)

    @staticmethod
    def _fail(resp: httpx.Response, what: str) -> EventSourceError:
        snippet = (resp.text or "")[:300]
        return EventSourceError(f"{what} returned {resp.status_code}: {snippet}", resp.status_code)

    @staticmethod
    def _rows(resp: httpx.Response) -> list[dict[str, Any]]:
        """The list out of a Xentral collection response, which wraps in
        ``{data: […]}`` on some endpoints and answers bare on others."""
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return []
        items = payload.get("data") if isinstance(payload, dict) else payload
        return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []

    # -- catalog ---------------------------------------------------------- #

    async def list_event_types(self, ctx: EventSourceContext) -> list[EventType]:
        """The events this tenant can subscribe to, live from the installation.

        Live rather than cached: the catalogue is per-installation and per
        version, so a stale list offers ids that cannot be subscribed — the
        exact failure a scraped catalogue caused before.
        """
        try:
            resp = await self._request(
                ctx, "GET", "/api/v1/webhookEventTypes", params={"include": "schema"}
            )
        except EventSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure is caller-visible
            raise EventSourceError(f"Could not load Xentral event types: {exc}", 502) from exc
        if resp.status_code >= 400:
            raise self._fail(resp, "GET /api/v1/webhookEventTypes")

        out: list[EventType] = []
        for row in self._rows(resp):
            event_id = str(row.get("id") or "").strip()
            if not event_id:
                continue
            # Verbatim, unreshaped — the same rule ``ParsedEvent`` states for
            # the payload. A schema we rewrote would describe our idea of the
            # delivery rather than Xentral's. Absent (older installation) and
            # explicitly null (event has no schema yet) both land as None; the
            # difference is not observable here and neither means "no fields".
            schema = row.get("schema")
            out.append(
                EventType(
                    id=event_id,
                    group=str(row.get("group") or ""),
                    schema=schema if isinstance(schema, dict) else None,
                )
            )
        return out

    # -- lifecycle -------------------------------------------------------- #

    async def prune_subscriptions_for(self, ctx: EventSourceContext, *, delivery_url: str) -> int:
        """Delete anything already registered against ``delivery_url``.

        Two concurrent activations of the same workflow both pass the "already
        subscribed" guard and both register; only the second id gets persisted,
        so the first becomes an orphan that delivers forever with no handle to
        remove it. Converging on the URL fixes that without a lock, and sweeps
        up orphans left by earlier crashed activations too.

        Best-effort: a tenant whose token cannot list webhooks still gets to
        subscribe. Failing the activation over a cleanup would be worse than
        the duplicate it prevents.
        """
        target = (delivery_url or "").rstrip("/")
        if not target:
            return 0
        try:
            listing = await self._request(ctx, "GET", "/api/v1/webhooks")
            if listing.status_code >= 400:
                raise self._fail(listing, "GET /api/v1/webhooks")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[erp-events] could not list Xentral webhooks for %s: %s", ctx.license_id, exc
            )
            return 0

        removed = 0
        for row in self._rows(listing):
            if str(row.get("url") or "").rstrip("/") != target:
                continue
            stale_id = str(row.get("id") or "")
            if not stale_id:
                continue
            try:
                resp = await self._request(ctx, "DELETE", f"/api/v1/webhooks/{stale_id}")
                if resp.status_code not in (200, 202, 204, 404):
                    raise self._fail(resp, f"DELETE /api/v1/webhooks/{stale_id}")
                removed += 1
                logger.info(
                    "[erp-events] removed stale Xentral webhook %s pointing at %s", stale_id, target
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[erp-events] could not remove stale webhook %s: %s", stale_id, exc)
        return removed

    async def subscribe(
        self,
        ctx: EventSourceContext,
        *,
        event_id: str,
        delivery_url: str,
        label: str,
    ) -> EventSubscription:
        """Register ``delivery_url`` for ``event_id`` and return the handle.

        Must not return without a usable ``subscription_id``: that id is the
        ONLY thing that can ever tear the subscription down again, so inventing
        one would leave it delivering forever (invariant ``I2``).
        """
        secret = secrets.token_urlsafe(_SECRET_BYTES)
        name = f"agent-hub:{(label or '')[:60]}" if label else "agent-hub"
        body = {
            "name": name,
            "url": delivery_url,
            "signatureKey": secret,
            "events": [{"id": event_id}] if event_id else [],
        }
        try:
            resp = await self._request(ctx, "POST", "/api/v1/webhooks", json_body=body)
        except EventSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EventSourceError(f"Could not register Xentral webhook: {exc}", 502) from exc
        if resp.status_code >= 400:
            raise self._fail(resp, "POST /api/v1/webhooks")

        subscription_id = _new_webhook_id(resp)
        if not subscription_id:
            raise EventSourceError(
                f"Xentral accepted the webhook ({resp.status_code}) but returned no id, so it "
                f"could never be removed again (Location={resp.headers.get('location')!r}).",
                502,
            )
        return EventSubscription(subscription_id=subscription_id, secret=secret)

    async def unsubscribe(self, ctx: EventSourceContext, *, subscription_id: str) -> None:
        """Remove the subscription. A 404 counts as success: the goal state is
        "not subscribed", and failing on a webhook someone deleted by hand in
        Xentral would strand our own record instead."""
        if not subscription_id:
            return
        try:
            resp = await self._request(ctx, "DELETE", f"/api/v1/webhooks/{subscription_id}")
        except EventSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EventSourceError(f"Could not remove Xentral webhook: {exc}", 502) from exc
        if resp.status_code not in (200, 202, 204, 404):
            raise self._fail(resp, f"DELETE /api/v1/webhooks/{subscription_id}")

    async def set_enabled(
        self, ctx: EventSourceContext, *, subscription_id: str, enabled: bool
    ) -> None:
        # Unreachable: callers gate on supports_pause. Raising rather than
        # passing silently keeps a future caller honest.
        raise NotImplementedError("Xentral webhooks cannot be suspended — delete and recreate.")

    # -- delivery --------------------------------------------------------- #

    def parse_delivery(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> ParsedEvent:
        """Verify the HMAC, then decode the envelope.

        Verification and decoding are fused by contract: a caller physically
        cannot obtain a payload without having authenticated it. The algorithm
        is documented in Xentral's public ``webhook-service`` repo
        (``internal/webhooks/domain/signature.go``):

            message   = body + str(timestamp)     # body bytes, then ASCII ts
            signature = hex(hmac_sha256(message, signatureKey))
        """
        if not secret:
            # Never compare against an empty key — that path would authenticate
            # a subscription whose secret was lost or never stored.
            raise EventSignatureError("no signature key configured for this subscription")

        timestamp = (headers.get(_TIMESTAMP_HEADER) or "").strip()
        signature = (headers.get(_SIGNATURE_HEADER) or "").strip()
        if not timestamp or not signature:
            raise EventSignatureError("signature or timestamp header missing")

        message = raw_body + timestamp.encode("ascii", errors="replace")
        expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected.lower()):
            raise EventSignatureError("signature mismatch")

        # Decode exactly as the receiver did before this source existed — same
        # expression, same fallback — so ``trigger.body`` is unchanged for every
        # workflow already authored against it.
        try:
            payload: Any = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = {"raw": raw_body.decode("utf-8", errors="replace")}

        event_id = ""
        if isinstance(payload, dict):
            event_id = str(payload.get("type") or "")
        return ParsedEvent(event_id=event_id, payload=payload)


def _new_webhook_id(resp: httpx.Response) -> str:
    """The id of a freshly created webhook.

    Per the Xentral spec the success response is ``201 Created`` with an EMPTY
    body and the new resource id in the ``Location`` header
    (``…/api/v1/webhooks/17``) — RFC 7231 says it is the URI of the new
    resource, so the last path segment is the id. Some versions (older Xentral,
    plugins) return a JSON body as well; that path is honoured too, because a
    missing id is not a cosmetic problem: it is a subscription nothing can ever
    remove.
    """
    from urllib.parse import urlparse

    try:
        if resp.content:
            data = resp.json()
            if isinstance(data, dict):
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                from_body = str(inner.get("id") or "")
                if from_body:
                    return from_body
    except Exception as exc:  # noqa: BLE001
        # The canonical 201 has an empty body, so this is the normal case, not a
        # problem. Logged at debug so a version that DOES answer with a body, in
        # an unexpected shape, stays diagnosable.
        logger.debug("[erp-events] no JSON body to read a webhook id from: %s", exc)

    location = resp.headers.get("location") or resp.headers.get("Location") or ""
    if not location:
        return ""
    try:
        path = urlparse(location).path.rstrip("/")
    except Exception:  # noqa: BLE001
        return ""
    return path.rsplit("/", 1)[-1] if path else ""


# Structural check: fail at import if the class drifts from the Protocol.
_: EventSource = XentralEventSource()
