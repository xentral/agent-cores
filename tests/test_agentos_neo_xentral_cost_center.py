"""CostCenter adapter: uuid-keyed reads, the name↔description alias, write mapping.

The three behaviours that are easy to break and expensive to notice:

* the BF read path is keyed by UUID — a numeric handle must be resolved through
  the index, because the create read-back re-reads by the numeric ``data.id``;
* the model calls the label ``name`` while upstream calls it ``description``, on
  reads, on writes AND in filter keys;
* a PATCH naming a readOnly property is accepted and silently dropped upstream,
  so the adapter must refuse unknown keys instead of forwarding them.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl, urlsplit

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.cost_center import CostCenterAdapter

BASE = "https://tenant.example"
PATH = "/api/entity/costCenter"
UUID = "019fd0e1-4d88-7064-a1da-e9b594e225db"

_ROW = {
    "number": "CC-100",
    "description": "Vertrieb",
    "internalNote": "note",
    "id": "8",
    "uuid": UUID,
    "createdAt": "2026-08-05T09:44:14+02:00",
    "updatedAt": "2026-08-05T09:44:14+02:00",
}


class Upstream:
    """Mirrors the measured BF behaviour: the collection lists, ``/{uuid}`` reads,
    ``/{numeric id}`` is a 404 — which is exactly what makes the index necessary."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == PATH:
            if request.method in ("POST",):
                return httpx.Response(201, json={"data": _ROW})
            return httpx.Response(self.status, json={"data": [_ROW], "meta": {"total": 1}})
        if path == f"{PATH}/{UUID}":
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(self.status, json={"data": _ROW})
        return httpx.Response(404, json={"message": f"Entity not found with uuid {path}"})


def _run(up: Upstream, *, method="GET", handle=None, query=(), body=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await CostCenterAdapter().request(
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


def _body(resp) -> dict:
    return json.loads(resp.content or b"{}")


def test_list_maps_description_to_name_and_uuid_to_speaking_id():
    resp = _run(Upstream())
    row = _body(resp)["data"][0]
    assert row["id"] == f"cc_{UUID}"
    assert row["name"] == "Vertrieb"
    assert row["number"] == "CC-100"
    assert "description" not in row


def test_get_by_speaking_id_reads_the_uuid_path_without_an_index_lookup():
    up = Upstream()
    resp = _run(up, handle=f"cc_{UUID}")
    assert resp.status_code == 200
    # One request only: the uuid needs no numeric→uuid resolution.
    assert [r.url.path for r in up.requests] == [f"{PATH}/{UUID}"]


def test_numeric_handle_is_resolved_through_the_uuid_index():
    """The create read-back re-reads by the NUMERIC id; without resolution the BF
    endpoint answers 404 and the create would fall back to the raw upstream row."""
    CostCenterAdapter._uuid_index.clear()
    up = Upstream()
    resp = _run(up, handle="cc_8")
    assert resp.status_code == 200
    assert _body(resp)["data"]["id"] == f"cc_{UUID}"
    # index build (collection) first, then the resolved uuid read
    assert [r.url.path for r in up.requests] == [PATH, f"{PATH}/{UUID}"]


def test_filter_on_name_is_translated_to_the_upstream_description_key():
    up = Upstream()
    _run(
        up,
        query=[
            ("filter[0][key]", "name"),
            ("filter[0][op]", "contains"),
            ("filter[0][value]", "Vert"),
        ],
    )
    assert _params(up.requests[0])["filter[0][key]"] == "description"


def test_sort_uses_the_bf_shape():
    up = Upstream()
    _run(up, query=[("sort", "-createdAt")])
    params = _params(up.requests[0])
    assert params["sort[0][key]"] == "createdAt"
    assert params["sort[0][direction]"] == "desc"
    # A flat `sort` key would 422 upstream, and no tiebreak may be appended.
    assert "sort" not in params


def test_undeclared_filter_is_refused_rather_than_silently_ignored():
    """Upstream accepts unknown query keys and answers with the UNFILTERED
    collection, which reads as a filtered result — so the guard must bite here."""
    up = Upstream()
    resp = _run(up, query=[("filter[0][key]", "internalNote"), ("filter[0][value]", "x")])
    assert resp.status_code == 422
    assert up.requests == []


def test_create_sends_upstream_keys_and_returns_the_model_shape():
    up = Upstream()
    resp = _run(
        up,
        method="POST",
        body=json.dumps({"number": "CC-100", "name": "Vertrieb", "internalNote": "note"}).encode(),
    )
    assert resp.status_code == 201
    sent = json.loads(up.requests[0].content)
    assert sent == {"number": "CC-100", "description": "Vertrieb", "internalNote": "note"}
    assert _body(resp)["data"]["id"] == f"cc_{UUID}"


def test_explicit_null_reaches_upstream_so_a_note_can_be_cleared():
    up = Upstream()
    _run(
        up,
        method="PATCH",
        handle=f"cc_{UUID}",
        body=json.dumps({"internalNote": None}).encode(),
    )
    assert json.loads(up.requests[0].content) == {"internalNote": None}


def test_unknown_field_is_refused_and_never_reaches_upstream():
    up = Upstream()
    resp = _run(
        up,
        method="PATCH",
        handle=f"cc_{UUID}",
        body=json.dumps({"name": "X", "bogusField": "x"}).encode(),
    )
    assert resp.status_code == 409
    assert _body(resp)["fields"] == ["bogusField"]
    assert up.requests == []


def test_echoed_readonly_fields_are_dropped_not_refused():
    """A read-modify-write caller sends the whole record back; its own id and
    timestamps must not turn into a 409."""
    up = Upstream()
    resp = _run(
        up,
        method="PATCH",
        handle=f"cc_{UUID}",
        body=json.dumps(
            {"object": "costCenter", "id": f"cc_{UUID}", "createdAt": "t", "name": "X"}
        ).encode(),
    )
    assert resp.status_code == 200
    assert json.loads(up.requests[0].content) == {"description": "X"}
