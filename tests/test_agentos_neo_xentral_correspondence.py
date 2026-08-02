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
                "kind": "note",
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
    # `isSent`/`isDeleted` are no longer forced on every create: they are part of
    # the delivery block a caller may set, and forcing them made an entry that
    # records a message ALREADY sent unrepresentable.
    assert "isSent" not in sent
    rec = json.loads(resp.content)["data"]
    assert rec["id"] == "cor_u-77"  # re-read by uuid, mapped


def test_create_rejects_a_kind_upstream_does_not_have():
    """The old model advertised eight kinds. Upstream's enum has five, and the
    other four answer "The selected type is invalid" — they came from the
    dissolved CRM tool and no record on the instance carries one."""
    up = Upstream({})
    for bad in ("ticket", "document", "follow_up", "appointment"):
        resp = _run(
            up,
            method="POST",
            body=json.dumps({"kind": bad, "customer": "cus_1"}).encode(),
        )
        assert resp.status_code == 409, bad
        assert "kind" in json.loads(resp.content)["fields"], bad
    assert up.requests == []  # never reached the upstream


def test_fax_is_the_kind_the_old_list_forgot():
    """`letter_fax` creates successfully upstream and was missing entirely."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.correspondence import (
        CorrespondenceAdapter,
    )

    wire, rejected = CorrespondenceAdapter().map_write({"kind": "fax"}, creating=False)
    assert wire["type"] == "letter_fax"
    assert not rejected
    kinds = {o["value"] for o in CorrespondenceAdapter().fields()["kind"]["options"]}
    assert kinds == {"email", "letter", "fax", "phone", "note"}


def test_update_and_delete_are_declared_now():
    """Upstream had full CRUD all along; only the mapping was missing."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.correspondence import (
        CorrespondenceAdapter,
    )

    ops = CorrespondenceAdapter().manifest.operations
    assert {"create", "update", "delete"} <= set(ops)


def test_the_sort_dialect_is_the_entity_api_pair_not_a_flat_key():
    """A flat `sort=createdAt` answers 422 "Each sort must have a string key and
    direction". This adapter used to read that as "no sort surface" and strip the
    key, so both directions came back in the same order — a `sort: fail` on a
    field that sorts perfectly well."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.correspondence import (
        CorrespondenceAdapter,
    )

    a = CorrespondenceAdapter()
    assert a.bf_sort is True

    up = Upstream({"/api/entity/correspondence": {"data": []}})
    _run(up, query=[("page[number]", "1"), ("page[size]", "5"), ("sort", "-createdAt")])
    params = dict(_params(up.requests[0]))
    assert params["sort[0][key]"] == "createdAt"
    assert params["sort[0][direction]"] == "desc"
    assert params["limit"] == "5" and params["offset"] == "0"
    assert "sort" not in params  # the flat key never goes out


def test_the_parties_and_delivery_blocks_map_both_ways():
    from xentral_entity_cores.agentos_neo_xentral.emulated.correspondence import (
        CorrespondenceAdapter,
    )

    a = CorrespondenceAdapter()
    d = a.map_read(
        {
            **_ROW,
            "senderAddress": {"id": "25"},
            "senderName": "VT",
            "senderCompany": "Xentral",
            "recipientEmail": "kunde@example.org",
            "recipientPostalAddress": {"name": "ACME", "city": "Berlin"},
            "sendAs": "pdf",
            "emailCc": "cc@example.org",
            "printer": 3,
        }
    )
    assert d["sender"]["partner"]["id"] == "cus_25"
    assert d["sender"]["company"] == "Xentral"
    assert d["recipient"]["email"] == "kunde@example.org"
    assert d["recipient"]["address"]["city"] == "Berlin"
    assert d["delivery"]["sendAs"] == "pdf" and d["delivery"]["printer"] == 3

    wire, rejected = a.map_write(
        {
            "sender": {"partner": {"id": "cus_25"}, "company": "Xentral"},
            "recipient": {"email": "kunde@example.org", "address": {"city": "Berlin"}},
            "delivery": {"sendAs": "pdf", "emailAddress": "x@example.org"},
        },
        creating=False,
    )
    assert not rejected
    assert wire["senderAddress"] == {"id": "25"}
    assert wire["recipientPostalAddress"] == {"city": "Berlin"}
    assert wire["email"] == "x@example.org"  # the second, undocumented column


def test_a_create_without_a_moment_gets_one():
    from xentral_entity_cores.agentos_neo_xentral.emulated.correspondence import (
        CorrespondenceAdapter,
    )

    wire, _ = CorrespondenceAdapter().map_write({"kind": "note"}, creating=True)
    assert wire["date"] and wire["time"]
    # ...but an update must not invent one
    wire, _ = CorrespondenceAdapter().map_write({"kind": "note"}, creating=False)
    assert "date" not in wire and "time" not in wire
