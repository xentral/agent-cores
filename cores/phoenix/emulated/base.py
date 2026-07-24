"""Phoenix core — live passthrough adapter base.

Phoenix (https://phoenix-backend.eu.xentral.dev) is a business-framework
backend that already speaks our wire dialect natively: ``GET /api/metadata``
serves the entity catalogue, ``GET /api/metadata/{key}`` the full field
contract (``rootNode.properties`` with ``embedded``/``collection``/
``reference`` nesting and per-field ``access``/``filterable``/``searchable``/
``sortable`` flags), and ``/api/entity/{key}`` full CRUD with the same
``filter[i][key|op|value]`` / ``sort[0][key|direction]`` / ``page[size|number]``
query conventions our gateway emits.

Nothing here is modeled by hand: ``metadata()`` fetches the upstream contract
live (with a short TTL cache so catalogue renders don't hammer the API) and
``request()`` / ``action()`` proxy every call verbatim. The only touches are
additive: ``creatable``/``updatable`` flags derived from upstream ``access``
(our consumers read those flag names), a ``general`` section fallback for
entities that declare none, and ``extra.total`` mirrored from the upstream
paginator ``meta.total`` on list responses.

Phoenix routes single-record reads by **uuid** (records carry both ``id`` and
``uuid``); the handle is passed through untouched, exactly like the native
Xentral ``/api/entity`` convention. The gateway's ``base_url`` / ``token`` are
the tenant's *Xentral* auth, the wrong system here, so they are dropped: the
Phoenix connection is resolved **per tenant from its integration account** (see
``PHOENIX_FIELDS`` and ``integrations.providers.phoenix``) — nothing is
hardcoded. When no account is configured the adapter returns the shared
"credentials missing" contract so the FE can prompt the user to connect it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from ...credentials import (
    CoreCredentialsMissing,
    CredentialField,
    error_payload as credentials_error_payload,
    register_core_fields,
    resolve_core_credentials,
)

logger = logging.getLogger(__name__)

# The per-tenant connection fields, stored as a first-party integration account
# under the ``phoenix`` connector (see integrations.providers.phoenix). Phoenix
# has no upstream auth yet, so only the base URL is required; an optional bearer
# token is sent when present (forward-compatible). Field names match the
# provider's credential_fields ``name``s.
PHOENIX_FIELDS: tuple[CredentialField, ...] = (
    CredentialField("phoenix_base_url", example="https://your-phoenix-host (base URL, no path)"),
    CredentialField(
        "phoenix_token",
        example="(nur falls die Instanz Auth verlangt)",
        secret=True,
        required=False,
    ),
)
# Let the core selector discover Phoenix's connection fields without importing
# this adapter module (it drives the "connect this core" hint on the card).
register_core_fields("phoenix", PHOENIX_FIELDS)

_METADATA_TTL_SECONDS = 300.0
_TIMEOUT_SECONDS = 15.0

# (base_url, entity key) -> (fetched_at, upstream /api/metadata/{key} payload).
# Keyed by host, not just key, so one tenant's Phoenix schema is never served
# to another tenant pointed at a different Phoenix instance.
_META_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_META_LOCK = threading.Lock()


def _phoenix_conn() -> tuple[str, str | None]:
    """Resolve ``(base_url, token|None)`` from the tenant's Phoenix integration
    account. Raises :class:`CoreCredentialsMissing` when none is configured."""
    creds = resolve_core_credentials("phoenix", PHOENIX_FIELDS)
    return creds["phoenix_base_url"].rstrip("/"), creds.get("phoenix_token")


# (base_url) -> (fetched_at, roster). The roster is the entity index only —
# (key, label, domain, operations) from GET /api/metadata. Keyed by host so one
# tenant's Phoenix catalogue is never served to another pointed elsewhere.
_ROSTER_CACHE: dict[str, tuple[float, tuple[tuple[str, str, str, tuple[str, ...]], ...]]] = {}
_ROSTER_LOCK = threading.Lock()


def fetch_phoenix_roster() -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    """The tenant's live Phoenix entity roster from ``GET /api/metadata``.

    Returns ``(key, label, domain, operations)`` per entity. This is the *only*
    thing the core would otherwise hardcode; fetching it live means a new
    ``#[BusinessEntity]`` in the Phoenix backend surfaces automatically. Degrades
    to an empty tuple when the tenant has no Phoenix connection configured, and
    to the last good roster (stale beats broken) on a transient fetch failure —
    it never raises, so it is safe on the synchronous composition path."""
    try:
        base_url, token = _phoenix_conn()
    except CoreCredentialsMissing:
        return ()
    now = time.monotonic()
    with _ROSTER_LOCK:
        cached = _ROSTER_CACHE.get(base_url)
        if cached and now - cached[0] < _METADATA_TTL_SECONDS:
            return cached[1]
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(f"{base_url}/api/metadata", timeout=_TIMEOUT_SECONDS, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("phoenix: roster fetch failed: %s", exc)
        with _ROSTER_LOCK:
            cached = _ROSTER_CACHE.get(base_url)
        return cached[1] if cached else ()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return ()
    roster = tuple(
        (
            str(row["key"]),
            str(row.get("label") or row["key"]),
            str(row.get("domain") or "general"),
            tuple(str(op) for op in (row.get("operations") or []) if op),
        )
        for row in rows
        if isinstance(row, dict) and row.get("key")
    )
    with _ROSTER_LOCK:
        _ROSTER_CACHE[base_url] = (now, roster)
    return roster


class PhoenixAdapterBase:
    """Live proxy to one Phoenix entity. Concrete adapters only carry the
    ``manifest``; schema and data always come from the upstream."""

    manifest: EmulationManifest

    # ---- schema -------------------------------------------------------------

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        try:
            base_url, token = _phoenix_conn()
        except CoreCredentialsMissing as exc:
            return self._credentials_missing_metadata(exc, accept_language)
        upstream = self._fetch_upstream_metadata(base_url, token)
        if upstream is None:
            return self._unreachable_metadata(accept_language, base_url)
        root = upstream.get("rootNode") or {}
        properties = {
            name: self._prop(raw)
            for name, raw in (root.get("properties") or {}).items()
            if isinstance(raw, dict)
        }
        sections = root.get("sections")
        if not isinstance(sections, dict) or not sections:
            sections = {"general": {"label": "General"}}
        operations = [str(op) for op in (upstream.get("operations") or [])]
        if "read" in operations and "list" not in operations:
            operations.insert(0, "list")
        meta: dict[str, Any] = {
            "key": self.manifest.key,
            "label": upstream.get("label") or self.manifest.label(accept_language),
            "operations": operations,
            "previewTemplateString": self._preview_template(properties),
            "sections": sections,
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }
        actions = root.get("actions")
        if actions:
            meta["actions"] = actions
        return meta

    def _fetch_upstream_metadata(self, base_url: str, token: str | None) -> dict[str, Any] | None:
        """The upstream per-entity contract, TTL-cached per host. A fetch failure
        falls back to the last good payload (stale beats broken); ``None`` only
        when the upstream was never reachable."""
        key = self.manifest.key
        cache_key = (base_url, key)
        now = time.monotonic()
        with _META_LOCK:
            cached = _META_CACHE.get(cache_key)
            if cached and now - cached[0] < _METADATA_TTL_SECONDS:
                return cached[1]
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = httpx.get(
                f"{base_url}/api/metadata/{key}",
                timeout=_TIMEOUT_SECONDS,
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("phoenix: metadata fetch failed for %s: %s", key, exc)
            with _META_LOCK:
                cached = _META_CACHE.get(cache_key)
            return cached[1] if cached else None
        if not isinstance(payload, dict):
            return None
        with _META_LOCK:
            _META_CACHE[cache_key] = (now, payload)
        return payload

    def _unreachable_metadata(self, accept_language: str | None, base_url: str) -> dict[str, Any]:
        return {
            "key": self.manifest.key,
            "label": self.manifest.label(accept_language),
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{id}}",
            "sections": {"general": {"label": "General"}},
            "rootNode": {"properties": {}},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
            "error": f"Phoenix backend not reachable ({base_url})",
        }

    def _credentials_missing_metadata(
        self, exc: CoreCredentialsMissing, accept_language: str | None
    ) -> dict[str, Any]:
        """Valid-but-empty schema carrying the credentials-missing contract, so
        the FE renders the "connect this core" hint instead of an empty table."""
        schema = {
            "key": self.manifest.key,
            "label": self.manifest.label(accept_language),
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{id}}",
            "sections": {"general": {"label": "General"}},
            "rootNode": {"properties": {}},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
            "error": "Phoenix-Verbindung ist nicht konfiguriert.",
        }
        schema.update(credentials_error_payload(exc))
        return schema

    def _credentials_missing_response(self, exc: CoreCredentialsMissing) -> AdapterResponse:
        body = json.dumps(
            {
                "title": "Phoenix-Verbindung ist nicht konfiguriert.",
                **credentials_error_payload(exc),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        # 424 Failed Dependency: config gap, not a backend error (502).
        return AdapterResponse(424, body, {"content-type": "application/json"})

    @classmethod
    def _prop(cls, raw: dict[str, Any]) -> dict[str, Any]:
        """Verbatim upstream property, plus the ``creatable``/``updatable``
        flags our consumers read (derived from upstream ``access``) and a
        ``general`` section fallback. Recurses through embedded ``properties``
        and collection ``node.properties`` — the nesting is load-bearing."""
        out = dict(raw)
        writable = raw.get("access") == "readWrite"
        out.setdefault("creatable", writable)
        out.setdefault("updatable", writable)
        if not out.get("section"):
            out["section"] = "general"
        nested = out.get("properties")
        if isinstance(nested, dict):
            out["properties"] = {
                name: cls._prop(sub) for name, sub in nested.items() if isinstance(sub, dict)
            }
        node = out.get("node")
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            out["node"] = {
                **node,
                "properties": {
                    name: cls._prop(sub)
                    for name, sub in node["properties"].items()
                    if isinstance(sub, dict)
                },
            }
        return out

    @staticmethod
    def _preview_template(properties: dict[str, Any]) -> str:
        for name, prop in properties.items():
            if prop.get("previewable"):
                return "{{" + name + "}}"
        return "{{id}}"

    # ---- data ---------------------------------------------------------------

    def _entity_url(self, base_url: str, handle: str | None = None) -> str:
        url = f"{base_url}/api/entity/{self.manifest.key}"
        return f"{url}/{handle}" if handle else url

    @staticmethod
    def _passthrough(resp: httpx.Response, *, is_list: bool = False) -> AdapterResponse:
        content = resp.content
        if is_list and resp.status_code == 200:
            # Additive: our consumers read the record total from
            # ``extra.total``; Phoenix reports it in the paginator ``meta``.
            try:
                payload = json.loads(content)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and "extra" not in payload:
                total = (payload.get("meta") or {}).get("total")
                if total is not None:
                    payload["extra"] = {"total": total}
                    content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(
            resp.status_code,
            content,
            {"content-type": resp.headers.get("content-type", "application/json")},
        )

    @staticmethod
    def _error(exc: Exception) -> AdapterResponse:
        body = json.dumps(
            {"title": f"Phoenix backend not reachable: {exc}"},
        ).encode("utf-8")
        return AdapterResponse(502, body, {"content-type": "application/json"})

    async def request(
        self,
        *,
        method: str,
        handle: str | None,
        query: list[tuple[str, str]],
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: Any | None = None,
    ) -> AdapterResponse:
        del base_url, token  # Xentral auth — Phoenix resolves its own from the vault.
        try:
            phx_base, phx_token = _phoenix_conn()
        except CoreCredentialsMissing as exc:
            return self._credentials_missing_response(exc)
        method = method.upper()
        headers = {"Accept": "application/json"}
        if phx_token:
            headers["Authorization"] = f"Bearer {phx_token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        if accept_language:
            headers["Accept-Language"] = accept_language
        is_list = method == "GET" and not handle
        try:
            if client is not None:
                resp = await client.request(
                    method,
                    self._entity_url(phx_base, handle),
                    params=query or None,
                    content=body,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as owned:
                    resp = await owned.request(
                        method,
                        self._entity_url(phx_base, handle),
                        params=query or None,
                        content=body,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            logger.warning("phoenix: %s %s failed: %s", method, self.manifest.key, exc)
            return self._error(exc)
        return self._passthrough(resp, is_list=is_list)

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: Any | None = None,
    ) -> AdapterResponse:
        """Actions proxy to Phoenix's declared-action endpoint. The gateway's
        BF ``{ids, command}`` envelope is Phoenix's native envelope too — the
        body passes through verbatim (``handle`` is always ``None``)."""
        del handle, base_url, token
        try:
            phx_base, phx_token = _phoenix_conn()
        except CoreCredentialsMissing as exc:
            return self._credentials_missing_response(exc)
        url = f"{self._entity_url(phx_base)}/actions/{action_key}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if phx_token:
            headers["Authorization"] = f"Bearer {phx_token}"
        if accept_language:
            headers["Accept-Language"] = accept_language
        try:
            if client is not None:
                resp = await client.request("PATCH", url, content=body or b"{}", headers=headers)
            else:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as owned:
                    resp = await owned.request("PATCH", url, content=body or b"{}", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "phoenix: action %s on %s failed: %s", action_key, self.manifest.key, exc
            )
            return self._error(exc)
        return self._passthrough(resp)
