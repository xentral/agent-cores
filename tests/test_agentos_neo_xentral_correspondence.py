"""Correspondence adapter: limit/offset dialect, filters, create policy.

The BF endpoint speaks ``limit``/``offset`` (not page[…]), filters on
``recipientAddress``/``type``/``date`` and rejects unknown query keys — the
adapter must never leak sort/page keys. Create is restricted to the manually
loggable kinds (ticket/document → 409 on ``type``).
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl, urlsplit

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.correspondence import (
    CorrespondenceAdapter,
)

BASE = "https://tenant.example"


class Upstream:
    def __init__(self, routes: dict[str, dict], status: int = 200):
        self.routes = routes
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = self.routes.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={"title": f"no route {request.url.path}"})
        return httpx.Response(self.status, json=payload)


def _run(up: Upstream, *, method="GET", handle=None, query=(), body=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await CorrespondenceAdapter().request(
                method=method,
                handle=handle,
                query=list(query),
                body=body,
                base_url=BASE,
                token="t",
                client=client,
            )

    return asyncio.run(go())


def _params(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(request.url)).query))


_ROW = {
    "id": "43",
    "uuid": "0195cb5b-c928-7bf9-a973-9ce799f95672",
    "type": "note",
    "subject": "Reklamation",
    "content": "Kunde reklamiert.",
    "date": "2026-05-06",
    "time": "14:30:00",
    "isSent": False,
    "recipientAddress": {"id": "20337", "name": "ACME"},
}


def test_list_translates_paging_and_strips_sort():
    up = Upstream({"/api/entity/correspondence": {"data": [_ROW]}})
    resp = _run(
        up,
        query=[
            ("page[number]", "3"),
            ("page[size]", "20"),
            ("sort", "-date"),
            ("filter[0][key]", "customer"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "cus_20337"),
        ],
    )
    p = _params(up.requests[0])
    assert p["limit"] == "20" and p["offset"] == "40"
    assert "sort" not in p and "page[number]" not in p
    # model key aliased + speaking prefix stripped
    assert p["filter[0][key]"] == "recipientAddress"
    assert p["filter[0][value]"] == "20337"
    rec = json.loads(resp.content)["data"][0]
    assert rec["id"] == "cor_0195cb5b-c928-7bf9-a973-9ce799f95672"
    assert rec["customer"]["id"] == "cus_20337"
    assert rec["customer"]["name"] == "ACME"


def test_read_by_speaking_id():
    # BF fetches by uuid — the speaking id encodes it (F3, like Tag).
    up = Upstream({"/api/entity/correspondence/0195cb5b-c928-7bf9-a973-9ce799f95672": {"data": _ROW}})
    resp = _run(up, handle="cor_0195cb5b-c928-7bf9-a973-9ce799f95672")
    assert json.loads(resp.content)["data"]["subject"] == "Reklamation"


def test_create_builds_upstream_payload_with_defaults():
    up = Upstream(
        {
            # POST answers with id AND uuid; the re-read must go by uuid
            # (GET by numeric id 404s upstream).
            "/api/entity/correspondence": {"data": {"id": "77", "uuid": "u-77"}},
            "/api/entity/correspondence/u-77": {"data": {**_ROW, "id": "77", "uuid": "u-77"}},
        }
    )
    resp = _run(
        up,
        method="POST",
        body=json.dumps(
            {
                "type": "note",
                "customer": {"id": "cus_20337"},
                "subject": "Notiz",
                "time": "09:15",
            }
        ).encode(),
    )
    assert resp.status_code == 201
    sent = json.loads(up.requests[0].content)
    assert sent["recipientAddress"] == {"id": "20337"}
    assert sent["type"] == "note"
    assert sent["time"] == "09:15:00"  # HH:MM padded
    assert sent["date"]  # defaulted to today
    assert sent["isSent"] is False and sent["isDeleted"] is False
    rec = json.loads(resp.content)["data"]
    assert rec["id"] == "cor_u-77"  # re-read by uuid, mapped


def test_create_rejects_integration_owned_types():
    up = Upstream({})
    for bad in ("ticket", "document"):
        resp = _run(
            up,
            method="POST",
            body=json.dumps({"type": bad, "customer": "cus_1"}).encode(),
        )
        assert resp.status_code == 409, bad
        assert "type" in json.loads(resp.content)["fields"], bad
    assert up.requests == []  # never reached the upstream


def test_update_and_delete_not_declared():
    up = Upstream({})
    assert _run(up, method="PATCH", handle="cor_43", body=b"{}").status_code == 405
    assert _run(up, method="DELETE", handle="cor_43").status_code == 405
    assert up.requests == []
