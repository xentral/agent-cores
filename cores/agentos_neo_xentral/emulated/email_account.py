"""Xentral V3 facade · emailAccount — the instance's mail accounts, with real sending.

Reads ``GET /api/v1/emailAccounts`` (undocumented in the public OpenAPI but
live — verified on mvp: id, name, email, type, tags, isSystemDefault,
projects; bare GET, ``/{id}`` works). Replaces the standalone ``xentral_email``
MCP tool: the account IS the action target, so the tool's account
disambiguation becomes picking a record (``isSystemDefault`` marks the
instance default).

The ``sendEmail`` action sends a REAL email —
``PATCH /api/v3/emailAccounts/{id}/actions/sendEmail`` (the exact payload the
dissolved tool and the job-approve flow use, incl. the retry without
``addSignature`` for older builds) — and then logs the correspondence entry on
the customer record (``POST /api/entity/correspondence``, the same endpoint the
Correspondence entity wraps; subject gets the ``OUT:`` outbound marker).

Xentral appends the sender's configured signature — the body must NOT contain
a closing block (said in the action description, carried over from the tool).
Attachment limits carried over too: base64-validated, 10 MB per file, 25 MB
combined, with a magic-byte sanity check for common types (a PDF read in text
mode produces valid base64 but garbage bytes — reject early, not at the SMTP
hop).
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, prop

_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_ATTACHMENTS_TOTAL_BYTES = 25 * 1024 * 1024
_MAGIC_BYTES: dict[str, tuple[str, list[bytes]]] = {
    ".pdf": ("PDF", [b"%PDF-"]),
    ".png": ("PNG", [b"\x89PNG\r\n\x1a\n"]),
    ".jpg": ("JPEG", [b"\xff\xd8\xff"]),
    ".jpeg": ("JPEG", [b"\xff\xd8\xff"]),
    ".zip": ("ZIP", [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"]),
    ".docx": ("DOCX", [b"PK\x03\x04"]),
    ".xlsx": ("XLSX", [b"PK\x03\x04"]),
}


def _normalize_attachments(attachments: Any) -> tuple[list[dict[str, str]], str | None]:
    """Validate + convert to Xentral's ``{fileName, fileContent}`` shape.
    Returns ``(normalized, error)`` — error is a user-facing message."""
    if not attachments:
        return [], None
    if not isinstance(attachments, list):
        return [], "attachments must be an array of {fileName, fileContent} objects."
    out: list[dict[str, str]] = []
    total = 0
    for i, att in enumerate(attachments):
        if not isinstance(att, dict):
            return [], f"attachments[{i}] must be an object."
        name = str(att.get("fileName") or att.get("file_name") or "").strip()
        content = att.get("fileContent") or att.get("file_content")
        if not name:
            return [], f"attachments[{i}].fileName is required."
        if not isinstance(content, str) or not content:
            return [], f"attachments[{i}].fileContent (base64 string) is required."
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as e:
            return [], f"attachments[{i}].fileContent is not valid base64: {e}"
        if len(raw) > _MAX_ATTACHMENT_BYTES:
            return [], f"attachments[{i}] '{name}' exceeds the 10 MB per-file limit."
        total += len(raw)
        if total > _MAX_ATTACHMENTS_TOTAL_BYTES:
            return [], f"attachments exceed the 25 MB combined limit at '{name}'."
        dot = name.rfind(".")
        entry = _MAGIC_BYTES.get(name[dot:].lower()) if dot >= 0 else None
        if entry and not any(raw.startswith(p) for p in entry[1]):
            return [], (
                f"'{name}' looks corrupted: decoded bytes are not a valid {entry[0]} "
                "signature — likely read in text mode before base64-encoding."
            )
        out.append({"fileName": name, "fileContent": content})
    return out, None


def _split_addresses(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


class EmailAccountAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="EmailAccount",
        label_en="Email account",
        category="settings",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.emailAccount",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),
    )
    v3_path = "/api/v1/emailAccounts"
    include = ""
    preview_template = "{{email}}"
    sort_tiebreak = None
    sections = {"general": {"label": "General"}}

    async def _get(
        self,
        base_url: str,
        token: str,
        *,
        handle: str | None,
        query: list[tuple[str, str]],
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> tuple[int, Any]:
        # Bare-GET endpoint: no paging/filter/sort params are accepted.
        if not handle:
            query = []
        return await super()._get(
            base_url,
            token,
            handle=handle,
            query=query,
            accept_language=accept_language,
            client=client,
        )

    def actions(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "sendEmail",
                "label": "Send email",
                "bulk": False,
                "method": "PATCH",
                "path": "/api/entity/EmailAccount/actions/sendEmail",
                "destructive": True,
                "description": (
                    "Send a REAL email from this account (auditable in Xentral, "
                    "irreversible — confirm intent first). Xentral appends the "
                    "sender's configured signature automatically: do NOT write a "
                    "closing block in `body`, end with the last sentence. Pass "
                    "`customer` (the Customer record id) whenever the recipient is "
                    "a known customer so the logged correspondence entry lands on "
                    "their CRM tab."
                ),
                "command": {
                    "type": "object",
                    "required": ["to", "subject", "body"],
                    "properties": {
                        "to": {"type": "string", "label": "To"},
                        "subject": {"type": "string", "label": "Subject"},
                        "body": {
                            "type": "string",
                            "label": "Body (HTML or plain text)",
                        },
                        "name": {"type": "string", "label": "Recipient display name"},
                        "cc": {"type": "string", "label": "CC (comma-separated)"},
                        "bcc": {"type": "string", "label": "BCC (comma-separated)"},
                        "customer": {
                            "type": "string",
                            "label": "Customer record id (cus_…) to log the entry on",
                        },
                        "attachments": {
                            "type": "array",
                            "label": "Attachments [{fileName, fileContent(base64)}]",
                        },
                    },
                },
            }
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "email": prop("string", "Email address", **RO, section="general", previewable=True),
            "type": prop("string", "Type", **RO, section="general"),
            "isSystemDefault": prop(
                "boolean",
                "Instance default",
                **RO,
                section="general",
                previewable=True,
                description="The account Xentral uses when no explicit sender is picked.",
            ),
            "projects": prop(
                "collection",
                "Projects",
                **RO,
                section="general",
                node={"properties": {"name": prop("string", "Name", **RO)}},
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "emailAccount",
            "id": (f"eml_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "email": r.get("email"),
            "type": r.get("type"),
            "isSystemDefault": r.get("isSystemDefault"),
            "projects": [
                {"name": p.get("name")} for p in (r.get("projects") or []) if isinstance(p, dict)
            ],
        }

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        if action_key != "sendEmail":
            return await super().action(
                action_key=action_key,
                handle=handle,
                body=body,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if not ids:
            return self._json(422, {"title": "sendEmail needs the sender account id (ids[])"})
        account_id = str(ids[0])
        if "_" in account_id:
            account_id = account_id.split("_", 1)[1]
        command = envelope.get("command") or {}
        to = str(command.get("to") or "").strip()
        subject = str(command.get("subject") or "").strip()
        body_html = command.get("body")
        if not to or not subject or not isinstance(body_html, str) or not body_html.strip():
            return self._json(422, {"title": "sendEmail requires command.to, .subject and .body."})
        attachments, att_error = _normalize_attachments(command.get("attachments"))
        if att_error:
            return self._json(422, {"title": att_error})

        payload: dict[str, Any] = {
            "to": to,
            "name": str(command.get("name") or ""),
            "subject": subject,
            "body": body_html,
            "addSignature": True,
        }
        cc = _split_addresses(command.get("cc"))
        if cc:
            payload["cc"] = cc
        bcc = _split_addresses(command.get("bcc"))
        if bcc:
            payload["bcc"] = bcc
        if attachments:
            payload["attachments"] = attachments

        headers = self._headers(token, accept_language)
        send_url = f"{base_url.rstrip('/')}/api/v3/emailAccounts/{account_id}/actions/sendEmail"

        async def _send(c: httpx.AsyncClient) -> httpx.Response:
            resp = await c.patch(send_url, json=payload, headers=headers)
            # Older builds reject `addSignature` — retry once without it.
            if resp.status_code not in (200, 204) and 400 <= resp.status_code < 500:
                fallback = {k: v for k, v in payload.items() if k != "addSignature"}
                resp = await c.patch(send_url, json=fallback, headers=headers)
            return resp

        async def _log_correspondence(c: httpx.AsyncClient) -> tuple[str | None, str | None]:
            now = datetime.now(ZoneInfo("Europe/Berlin"))
            corr: dict[str, Any] = {
                "type": "email",
                "recipientEmail": to,
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                # `OUT:` marks the entry as outbound in the CRM tab; the actual
                # email keeps the unprefixed subject.
                "subject": f"OUT: {subject}",
                "content": body_html,
                "hasSignature": False,
                "sendAs": "email",
                "isFax": False,
                "isSent": True,
                "isDeleted": False,
            }
            customer = command.get("customer")
            customer_id = customer.get("id") if isinstance(customer, dict) else customer
            if isinstance(customer_id, str) and "_" in customer_id:
                customer_id = customer_id.split("_", 1)[1]
            if customer_id:
                corr["recipientAddress"] = {"id": str(customer_id)}
            resp = await c.post(
                f"{base_url.rstrip('/')}/api/entity/correspondence", json=corr, headers=headers
            )
            if resp.status_code in (200, 201):
                try:
                    data = (resp.json() or {}).get("data") or {}
                except ValueError:
                    data = {}
                cid = data.get("uuid") or data.get("id")
                return (f"cor_{cid}" if cid else None), None
            return None, f"correspondence log returned {resp.status_code}"

        async def _run(c: httpx.AsyncClient) -> AdapterResponse:
            resp = await _send(c)
            if resp.status_code not in (200, 204):
                return AdapterResponse(
                    resp.status_code, resp.content, {"content-type": "application/json"}
                )
            correspondence_id, warning = await _log_correspondence(c)
            out: dict[str, Any] = {
                "data": {
                    "sent": True,
                    "account": f"eml_{account_id}",
                    "to": to,
                    "correspondence": correspondence_id,
                    "message": f"Email sent to {to} via account eml_{account_id}.",
                }
            }
            if warning:
                out["data"]["warning"] = warning
            return self._json(200, out)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                return await _run(c)
        return await _run(client)
