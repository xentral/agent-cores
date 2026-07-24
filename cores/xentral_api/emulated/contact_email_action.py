from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse

_TIMEOUT_SECONDS = 60.0


def send_email_action_metadata(entity_key: str) -> dict[str, Any]:
    return {
        "key": "sendEmail",
        "label": "Send email",
        "bulk": False,
        "method": "PATCH",
        "path": f"/api/entity/{entity_key}/actions/sendEmail",
        "destructive": True,
        "description": "Send an email to this business partner through a Xentral email account.",
        "command": {
            "type": "object",
            "required": ["emailAccountId", "subject", "body"],
            "properties": {
                "emailAccountId": {"type": "string", "label": "Email account ID"},
                "to": {"type": "string", "label": "Recipient email"},
                "name": {"type": "string", "label": "Recipient name"},
                "cc": {"type": "string", "label": "CC"},
                "bcc": {"type": "string", "label": "BCC"},
                "subject": {"type": "string", "label": "Subject"},
                "body": {"type": "string", "label": "Body"},
                "addSignature": {"type": "boolean", "label": "Add signature"},
                "attachments": {
                    "type": "collection",
                    "label": "Attachments",
                    "node": {
                        "properties": {
                            "fileName": {"type": "string", "label": "File name"},
                            "fileContent": {"type": "string", "label": "Base64 content"},
                        }
                    },
                },
            },
        },
    }


def _json_response(status_code: int, payload: dict[str, Any]) -> AdapterResponse:
    return AdapterResponse(
        status_code,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"content-type": "application/json"},
    )


def _split_email_list(value: Any) -> list[str] | Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return deepcopy(value)


def _raw_recipient(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    email = raw.get("email")
    if email in (None, "") and isinstance(raw.get("communication"), dict):
        email = raw["communication"].get("email")
    name = raw.get("name")
    if name in (None, ""):
        name = (
            " ".join(part for part in (raw.get("firstname"), raw.get("lastname")) if part) or None
        )
    return (
        str(email) if email not in (None, "") else None,
        str(name) if name not in (None, "") else None,
    )


async def execute_send_email_action(
    *,
    action_key: str,
    body: bytes | None,
    base_url: str,
    token: str,
    accept_language: str | None,
    client: httpx.AsyncClient | None,
    entity_key: str,
    entity_label: str,
    base_path: str,
    detail_include: str,
    request_headers: Callable[[str, str | None], dict[str, str]],
) -> AdapterResponse:
    if action_key != "sendEmail":
        return _json_response(404, {"message": f"Unknown {entity_key} action: {action_key}"})
    try:
        envelope = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _json_response(422, {"message": f"Invalid action envelope: {exc}"})
    if not isinstance(envelope, dict):
        return _json_response(422, {"message": "Action envelope must be a JSON object."})
    ids = envelope.get("ids")
    if not isinstance(ids, list) or not ids:
        return _json_response(422, {"message": "ids must be a non-empty array."})
    if len(ids) != 1:
        return _json_response(422, {"message": "sendEmail only supports one recipient id."})
    recipient_id = str(ids[0]).strip()
    if not recipient_id.isdigit():
        return _json_response(422, {"message": "Recipient id must be numeric."})
    command = envelope.get("command") or {}
    if not isinstance(command, dict):
        return _json_response(422, {"message": "command must be an object."})

    email_account_id = str(command.get("emailAccountId") or "").strip()
    if not email_account_id:
        return _json_response(422, {"message": "command.emailAccountId is required."})
    subject = command.get("subject")
    body_text = command.get("body")
    if subject in (None, ""):
        return _json_response(422, {"message": "command.subject is required."})
    if body_text in (None, ""):
        return _json_response(422, {"message": "command.body is required."})

    headers = request_headers(token, accept_language)

    async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
        to = command.get("to") or command.get("recipientEmail")
        name = command.get("name") or command.get("recipientName")
        if to in (None, ""):
            recipient_response = await request_client.get(
                f"{base_url.rstrip('/')}{base_path}/{recipient_id}",
                params={"include": detail_include},
                headers=headers,
            )
            if recipient_response.status_code >= 400:
                return recipient_response
            try:
                raw = recipient_response.json().get("data", {})
            except (AttributeError, ValueError):
                raw = {}
            default_to, default_name = _raw_recipient(raw if isinstance(raw, dict) else {})
            to = default_to
            if name in (None, ""):
                name = default_name
        if to in (None, ""):
            request = httpx.Request(
                "PATCH",
                f"{base_url.rstrip('/')}/api/v3/emailAccounts/{email_account_id}/actions/sendEmail",
            )
            return httpx.Response(
                422,
                request=request,
                json={
                    "message": f"{entity_label} {recipient_id} has no email address. Pass command.to explicitly."
                },
            )

        payload: dict[str, Any] = {
            "to": to,
            "subject": subject,
            "body": body_text,
        }
        if name not in (None, ""):
            payload["name"] = name
        for key in ("cc", "bcc"):
            value = command.get(key)
            if value not in (None, ""):
                payload[key] = _split_email_list(value)
        if "addSignature" in command:
            payload["addSignature"] = bool(command["addSignature"])
        if command.get("attachments") not in (None, ""):
            payload["attachments"] = deepcopy(command["attachments"])

        return await request_client.patch(
            f"{base_url.rstrip('/')}/api/v3/emailAccounts/{email_account_id}/actions/sendEmail",
            json=payload,
            headers=headers,
        )

    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
            response = await _perform(request_client)
    else:
        response = await _perform(client)

    if response.status_code >= 400:
        return AdapterResponse(response.status_code, response.content, dict(response.headers))
    return _json_response(
        200,
        {
            "message": f"Email sent to {entity_label} {recipient_id}.",
            "data": {"id": recipient_id, "emailAccountId": email_account_id},
        },
    )
