"""Product BOM write: bom.items is composed on /products/{id}/parts.

The v2 product body cannot carry parts, so — like the sale price — providing
``bom.items`` on a create/update SETS the product's bill of materials via the
/parts sub-resource: POST the desired parts, then DELETE the previously existing
lines (POST-before-DELETE, so a failed POST leaves the old BOM intact). These pin
that flow, the map_write pass-through (bom never leaks into the v2 body), and the
honest partial-success warning.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter


def test_bom_not_rejected_and_never_in_v2_body():
    v2, rejected = ProductAdapter().map_write(
        {"name": "Kit", "bom": {"items": [{"product": {"id": "prd_18"}, "quantity": 2}]}},
        creating=True,
    )
    assert rejected == set()  # bom is accepted (composed separately)
    assert "bom" not in v2 and "parts" not in v2  # never sent in the product body


class _Up:
    """Fake Xentral: v2 products write + v3 read-back + v1/v2 parts sub-resource."""

    def __init__(
        self, *, existing_parts: list[dict[str, Any]] | None = None, parts_post_status: int = 201
    ):
        self.existing = existing_parts or []
        self.parts_post_status = parts_post_status
        self.parts_posts: list[Any] = []
        self.parts_deletes: list[Any] = []
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.paths.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else None
        if method == "POST" and path == "/api/v2/products":
            return httpx.Response(201, json={"data": {"id": "777"}})
        if method == "PATCH" and path == "/api/v2/products/777":
            return httpx.Response(204)
        if method == "GET" and path == "/api/v3/products/777":
            return httpx.Response(200, json={"data": {"id": 777, "name": "Kit", "number": "K-1"}})
        if method == "GET" and path == "/api/v1/products/777/parts":
            return httpx.Response(200, json={"data": self.existing})
        if method == "POST" and path == "/api/v2/products/777/parts":
            self.parts_posts.append(body)
            if self.parts_post_status >= 400:
                return httpx.Response(self.parts_post_status, json={"title": "parts rejected"})
            return httpx.Response(201, headers={"Location": "/api/v2/products/777/parts/9"})
        if method == "DELETE" and path == "/api/v1/products/777/parts":
            self.parts_deletes.append(body)
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {method} {path}")


def _write(up: _Up, *, method: str, handle: str | None, model: dict[str, Any]):
    a = ProductAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method=method,
                handle=handle,
                query=[],
                body=json.dumps(model).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


_MODEL = {
    "name": "Kit",
    "bom": {
        "items": [
            {"product": {"id": "prd_18"}, "quantity": 2, "type": "shopping part"},
            {"product": {"id": "prd_19"}, "quantity": 1, "reference": "R-9"},
        ]
    },
}


def test_create_composes_parts_and_stamps_response():
    up = _Up()  # no existing parts
    resp = _write(up, method="POST", handle=None, model=_MODEL)
    assert resp.status_code == 201
    # parts POSTed with the v2 shape (part/amount/type/reference), no DELETE (nothing old)
    assert up.parts_posts[0] == [
        {"part": {"id": "18"}, "amount": 2, "type": "shopping part"},
        {"part": {"id": "19"}, "amount": 1, "reference": "R-9"},
    ]
    assert up.parts_deletes == []
    data = json.loads(resp.content)["data"]
    assert [i["product"]["id"] for i in data["bom"]["items"]] == ["prd_18", "prd_19"]
    assert data["bom"]["items"][0]["quantity"] == 2
    assert "_warnings" not in data


def test_update_replaces_existing_parts_post_before_delete():
    up = _Up(
        existing_parts=[
            {"id": "300", "product": {"id": "5"}, "amount": "9"},
            {"id": "301", "product": {"id": "6"}, "amount": "1"},
        ]
    )
    resp = _write(up, method="PATCH", handle="prd_777", model=_MODEL)
    assert resp.status_code == 200
    # new parts posted, THEN the two old lines deleted by id
    assert up.parts_posts and up.parts_deletes == [[{"id": "300"}, {"id": "301"}]]
    # ordering: POST precedes DELETE (non-destructive on a failed POST)
    assert up.paths.index("POST /api/v2/products/777/parts") < up.paths.index(
        "DELETE /api/v1/products/777/parts"
    )


def test_empty_bom_clears_parts():
    up = _Up(existing_parts=[{"id": "300", "product": {"id": "5"}, "amount": "9"}])
    resp = _write(up, method="PATCH", handle="prd_777", model={"name": "Kit", "bom": {"items": []}})
    assert resp.status_code == 200
    assert up.parts_posts == []  # nothing to add
    assert up.parts_deletes == [[{"id": "300"}]]  # old line cleared


def test_bom_failure_is_a_partial_success_warning():
    up = _Up(parts_post_status=400)
    resp = _write(up, method="POST", handle=None, model=_MODEL)
    assert resp.status_code == 201  # product created — not a hard error
    data = json.loads(resp.content)["data"]
    assert "bom" in data["_warnings"]
    assert up.parts_deletes == []  # POST failed → old BOM untouched (nothing deleted)
