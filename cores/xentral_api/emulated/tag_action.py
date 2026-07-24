from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse

_TIMEOUT_SECONDS = 60.0


def tag_action_metadata(entity_key: str, action_key: str) -> dict[str, Any]:
    label = "Add tag" if action_key == "addTag" else "Remove tag"
    description = (
        "Add a tag to this record (created automatically if new)."
        if action_key == "addTag"
        else "Remove a tag from this record."
    )
    return {
        "key": action_key,
        "label": label,
        "bulk": False,
        "method": "PATCH",
        "path": f"/api/entity/{entity_key}/actions/{action_key}",
        "destructive": False,
        "description": description,
        "command": {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string", "label": "Tag"}},
        },
    }


def _tag_name(tag: Any) -> str | None:
    if isinstance(tag, dict):
        value = tag.get("name") or tag.get("title")
        return str(value) if value else None
    if isinstance(tag, str) and tag:
        return tag
    return None


def _normalized_tag_payload(tag: Any) -> dict[str, Any] | None:
    if isinstance(tag, dict):
        out = deepcopy(tag)
        if out.get("id") is not None:
            out["id"] = str(out["id"])
        if "name" not in out and out.get("title"):
            out["name"] = out.pop("title")
        return out if out.get("name") else None
    if isinstance(tag, str) and tag:
        return {"name": tag}
    return None


async def execute_tag_action(
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
    json_response: Callable[[int, dict[str, Any]], AdapterResponse],
    # v2-style resources (products) have NO per-record PATCH — updates go
    # through the collection endpoint as a bulk array of {id, ...} items.
    bulk_update: bool = False,
) -> AdapterResponse:
    if action_key not in {"addTag", "removeTag"}:
        return json_response(404, {"message": f"Unknown {entity_key} action: {action_key}"})
    try:
        envelope = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return json_response(422, {"message": f"Invalid action envelope: {exc}"})
    if not isinstance(envelope, dict):
        return json_response(422, {"message": "Action envelope must be a JSON object."})
    ids = envelope.get("ids")
    if not isinstance(ids, list) or not ids:
        return json_response(422, {"message": "ids must be a non-empty array."})
    if len(ids) != 1:
        return json_response(422, {"message": f"{action_key} only supports one record id."})
    record_id = str(ids[0]).strip()
    if not record_id.isdigit():
        return json_response(422, {"message": "Record id must be numeric."})
    command = envelope.get("command") or {}
    if not isinstance(command, dict):
        return json_response(422, {"message": "command must be an object."})
    title = str(command.get("title") or command.get("name") or "").strip()
    if not title:
        return json_response(422, {"message": f"{action_key} needs a tag title."})

    headers = request_headers(token, accept_language)
    record_url = f"{base_url.rstrip('/')}{base_path}/{record_id}"

    async def _run(request_client: httpx.AsyncClient) -> AdapterResponse:
        current = await request_client.get(
            record_url,
            params={"include": detail_include},
            headers=headers,
        )
        if current.status_code >= 400:
            return AdapterResponse(current.status_code, current.content, dict(current.headers))
        try:
            body_json = current.json() if current.content else {}
        except ValueError:
            return AdapterResponse(current.status_code, current.content, dict(current.headers))
        record = body_json.get("data", body_json) if isinstance(body_json, dict) else {}
        existing = record.get("tags") if isinstance(record, dict) else []
        tags = [
            normalized
            for tag in (existing if isinstance(existing, list) else [])
            if (normalized := _normalized_tag_payload(tag)) is not None
        ]
        names = [_tag_name(tag) for tag in tags]
        if action_key == "addTag":
            if title not in names:
                tags.append({"name": title})
        else:
            tags = [tag for tag in tags if _tag_name(tag) != title]

        if bulk_update:
            # The bulk item schema is strict (additionalProperties: false, tag
            # entries allow only id/key/name) — slim each entry down to its
            # identifier so read-along fields can't fail validation.
            slim_tags = [
                {"id": str(tag["id"])} if tag.get("id") is not None else {"name": tag["name"]}
                for tag in tags
            ]
            patched = await request_client.patch(
                f"{base_url.rstrip('/')}{base_path}",
                json=[{"id": record_id, "tags": slim_tags}],
                headers=headers,
            )
        else:
            patched = await request_client.patch(record_url, json={"tags": tags}, headers=headers)
        if patched.status_code >= 400:
            return AdapterResponse(patched.status_code, patched.content, dict(patched.headers))
        verb = "added to" if action_key == "addTag" else "removed from"
        return json_response(
            200,
            {
                "message": f"Tag '{title}' {verb} {entity_label} {record_id}.",
                "data": {"id": record_id, "tags": tags},
            },
        )

    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
            return await _run(request_client)
    return await _run(client)
