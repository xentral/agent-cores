"""Product custom-field VALUES (Freifelder) and property VALUES (Eigenschaften).

Both were previously unmodelled. Custom-field values now READ from the v3
``include=customFields`` payload and WRITE via the v2 body ``freeFields[{id, value}]``.
Property values READ (hydrate) from v1 /properties and WRITE via v1 PATCH /properties
composed on top of the product write.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.product import (
    ProductAdapter,
    map_custom_fields,
    map_properties,
)


def test_map_custom_fields_maps_key_to_slot_number():
    got = map_custom_fields([{"key": "customField3", "label": "Material", "value": "Alu"}])
    assert got == [{"number": 3, "label": "Material", "value": "Alu"}]


def test_map_properties_maps_property_ref_and_value():
    got = map_properties(
        {
            "data": [
                {"id": "1", "property": {"id": "5", "name": "Color"}, "value": "blue", "unit": None}
            ]
        }
    )
    assert got[0]["property"]["id"] == "pprop_5"
    assert got[0]["name"] == "Color"
    assert got[0]["value"] == "blue"


def test_map_write_custom_fields_to_v2_free_fields():
    v2, rejected = ProductAdapter().map_write(
        {"customFields": [{"number": 1, "value": "Red"}, {"number": 40, "value": "X"}]},
        creating=False,
    )
    assert rejected == set()
    assert v2["freeFields"] == [{"id": "1", "value": "Red"}, {"id": "40", "value": "X"}]


def test_map_write_properties_are_not_in_the_v2_body():
    v2, rejected = ProductAdapter().map_write(
        {"name": "X", "properties": [{"property": {"id": "pprop_5"}, "value": "blue"}]},
        creating=True,
    )
    assert rejected == set()  # accepted (composed separately)
    assert "properties" not in v2


class _Up:
    def __init__(self, *, props_status: int = 204):
        self.props_status = props_status
        self.product_posts: list[dict[str, Any]] = []
        self.property_patches: list[Any] = []
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.paths.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else None
        if method == "POST" and path == "/api/v2/products":
            self.product_posts.append(body)
            return httpx.Response(201, json={"data": {"id": "777"}})
        if method == "GET" and path == "/api/v3/products/777":
            return httpx.Response(
                200, json={"data": {"id": 777, "name": "Widget", "number": "W-1"}}
            )
        if method == "PATCH" and path == "/api/v1/products/777/properties":
            self.property_patches.append(body)
            if self.props_status >= 400:
                return httpx.Response(self.props_status, json={"title": "props rejected"})
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {method} {path}")


def _create(up: _Up, model: dict[str, Any]):
    a = ProductAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="POST",
                handle=None,
                query=[],
                body=json.dumps(model).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


_MODEL = {
    "name": "Widget",
    "customFields": [{"number": 2, "value": "Aluminium"}],
    "properties": [
        {"property": {"id": "pprop_5"}, "value": "blue"},
        {"property": {"id": "pprop_6"}, "value": "30", "unit": "cm"},
    ],
}


def test_create_writes_free_fields_in_body_and_composes_properties():
    up = _Up()
    resp = _create(up, _MODEL)
    assert resp.status_code == 201
    # free-field values go in the v2 product body
    assert up.product_posts[0]["freeFields"] == [{"id": "2", "value": "Aluminium"}]
    # property values are PATCHed to the /properties sub-resource (prefix stripped)
    assert up.property_patches[0] == [
        {"property": {"id": "5"}, "value": "blue"},
        {"property": {"id": "6"}, "value": "30", "unit": "cm"},
    ]
    data = json.loads(resp.content)["data"]
    assert [p["property"]["id"] for p in data["properties"]] == ["pprop_5", "pprop_6"]
    assert "_warnings" not in data


def test_property_write_failure_is_a_partial_success_warning():
    up = _Up(props_status=400)
    resp = _create(up, _MODEL)
    assert resp.status_code == 201  # product created — not a hard error
    data = json.loads(resp.content)["data"]
    assert "properties" in data["_warnings"]
