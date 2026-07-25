"""addTag/removeTag must be effect-checked, not trusted on HTTP 200.

Current Xentral builds auto-create an unknown tag title on the v3 tags write,
but OLDER builds answer 200 and silently DROP titles missing from the tag
catalogue — the action's contract ("created automatically if new") broke
without any error. The facade therefore verifies the read-back after the
PATCH; when the tag did not stick it creates the catalogue entry via
``POST /api/entity/tag`` (label + slug, verified live) and retries once, and
answers an honest 502 instead of a false success when the change still does
not persist.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest
from xentral_entity_cores.agentos_neo_xentral.emulated.base import (
    FacadeAdapterBase,
    map_tags,
    tags_prop,
    tags_to_v3,
)


class _ThingAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Thing",
        label_en="Thing",
        category="sales",
        rollout_batch="test",
        adapter="test.thing",
        source_apis=("test",),
        operations=("list", "read", "update"),
    )
    v3_path = "/api/v3/things"
    sections = {"general": {"label": "General"}}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {"id": {"type": "string", "label": "ID"}, "tags": tags_prop(writable=True)}

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(r.get("id")), "tags": map_tags(r.get("tags"))}

    def map_write(self, model: dict[str, Any], *, creating: bool):
        v3: dict[str, Any] = {}
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        return v3, {k for k in model if k != "tags"}


class _Upstream:
    """Stateful fake Xentral: one record, a tag catalogue, and a switch for the
    old-build behavior (PATCH silently drops titles missing from the catalogue).
    """

    def __init__(self, *, auto_create: bool, catalogue_create_fails: bool = False):
        self.auto_create = auto_create
        self.catalogue_create_fails = catalogue_create_fails
        self.catalogue: set[str] = {"existing"}
        self.record_tags: list[str] = ["existing"]
        self.catalogue_posts: list[dict[str, Any]] = []
        self.patches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/entity/tag" and request.method == "POST":
            body = json.loads(request.content)
            self.catalogue_posts.append(body)
            if self.catalogue_create_fails:
                return httpx.Response(400, json={"title": "slug already exists."})
            self.catalogue.add(body["label"])
            return httpx.Response(201, json={"data": {"id": "9", "label": body["label"]}})
        if path == "/api/v3/things/1" and request.method == "GET":
            return httpx.Response(
                200,
                json={"data": {"id": "1", "tags": [{"title": t} for t in self.record_tags]}},
            )
        if path == "/api/v3/things/1" and request.method == "PATCH":
            self.patches += 1
            wanted = [t["title"] for t in json.loads(request.content).get("tags") or []]
            if self.auto_create:
                self.catalogue.update(wanted)
            # old builds keep only catalogue-known titles — silently, with a 200
            self.record_tags = [t for t in wanted if t in self.catalogue]
            return httpx.Response(200, json={"data": {"id": "1"}})
        raise AssertionError(f"unexpected call: {request.method} {path}")


def _run(upstream: _Upstream, action_key: str, title: str):
    adapter = _ThingAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)) as client:
            return await adapter.action(
                action_key=action_key,
                handle="1",
                body=json.dumps({"ids": ["1"], "command": {"title": title}}).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def test_add_tag_new_build_auto_creates_no_catalogue_call():
    up = _Upstream(auto_create=True)
    resp = _run(up, "addTag", "brand-new")
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"]["tags"] == ["existing", "brand-new"]
    assert up.catalogue_posts == []  # happy path stays a single write
    assert up.patches == 1


def test_add_tag_old_build_silent_drop_is_repaired_via_catalogue_create():
    up = _Upstream(auto_create=False)
    resp = _run(up, "addTag", "Brand New!")
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"]["tags"] == ["existing", "Brand New!"]
    assert up.catalogue_posts == [{"label": "Brand New!", "slug": "brand-new"}]
    assert up.patches == 2  # initial write + retry after the catalogue create


def test_add_tag_still_dropped_answers_honest_error():
    up = _Upstream(auto_create=False, catalogue_create_fails=True)
    resp = _run(up, "addTag", "brand-new")
    assert resp.status_code == 502
    payload = json.loads(resp.content)
    assert "did not attach tag 'brand-new'" in payload["title"]
    assert "slug already exists" in payload["detail"]
    assert up.record_tags == ["existing"]  # nothing pretended, nothing changed


def test_add_tag_existing_title_untouched_by_effect_check():
    up = _Upstream(auto_create=False)
    resp = _run(up, "addTag", "existing")
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"]["tags"] == ["existing"]
    assert up.catalogue_posts == []


def test_remove_tag_effect_checked():
    up = _Upstream(auto_create=False)
    resp = _run(up, "removeTag", "existing")
    assert resp.status_code == 200
    assert json.loads(resp.content)["data"]["tags"] == []
