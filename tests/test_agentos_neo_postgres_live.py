"""agentos_neo_postgres — live CRUD roundtrip against a real Postgres.

Gated on the ``NEO_PG_TEST_HOST`` env (plus DB/user/password); skipped
otherwise so CI without a database stays green. Run locally e.g. with

    NEO_PG_TEST_HOST=localhost NEO_PG_TEST_DB=agentos_neo_db \
    NEO_PG_TEST_USER=agentos_neo NEO_PG_TEST_PASSWORD=… pytest tests/test_agentos_neo_postgres_live.py

Effect-based (see the repo's testing rules): every write is verified by
reading the effect back, never by trusting the status code alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from xentral_entity_cores.agentos_neo_postgres.emulated import base as pg_base
from xentral_entity_cores.agentos_neo_postgres.emulated import build_adapters

HOST = os.environ.get("NEO_PG_TEST_HOST")

pytestmark = pytest.mark.skipif(not HOST, reason="NEO_PG_TEST_HOST not set — live DB test")

ADAPTERS = {a.manifest.key: a for a in build_adapters()}


@pytest.fixture(autouse=True)
def _config_override():
    pg_base._CONFIG_OVERRIDE = {
        "pg_host": HOST or "",
        "pg_port": os.environ.get("NEO_PG_TEST_PORT", "5432"),
        "pg_database": os.environ.get("NEO_PG_TEST_DB", "agentos_neo_db"),
        "pg_user": os.environ.get("NEO_PG_TEST_USER", "agentos_neo"),
        "pg_password": os.environ.get("NEO_PG_TEST_PASSWORD", ""),
        "pg_sslmode": "",
    }
    yield
    pg_base._CONFIG_OVERRIDE = None


def _call(adapter, **kw):
    kw.setdefault("query", [])
    kw.setdefault("body", None)
    kw.setdefault("handle", None)
    resp = asyncio.run(adapter.request(base_url="", token="", **kw))
    return resp.status_code, json.loads(resp.content or b"{}")


def _sql(coro_fn):
    async def _run():
        pool = await pg_base._get_pool(pg_base._config())
        return await coro_fn(pool)

    return asyncio.run(_run())


def test_full_crud_roundtrip_with_effects():
    customer = ADAPTERS["Customer"]
    marker = f"live-test-{uuid.uuid4().hex[:8]}"
    rec_id = None
    try:
        # create → effect: readable, listable by filter, counted in extra.total
        st, created = _call(customer, method="POST", body=json.dumps({"name": marker}).encode())
        assert st == 201, created
        rec_id = created["data"]["id"]
        assert rec_id.startswith("cus_")
        assert created["data"]["name"] == marker
        assert created["data"].get("number"), "number should be auto-assigned"

        st, read = _call(customer, method="GET", handle=rec_id)
        assert st == 200 and read["data"]["name"] == marker

        st, listed = _call(
            customer,
            method="GET",
            query=[
                ("filter[0][key]", "name"),
                ("filter[0][op]", "equals"),
                ("filter[0][value]", marker),
            ],
        )
        assert st == 200
        assert [r["id"] for r in listed["data"]] == [rec_id]
        assert listed["extra"]["total"] == 1

        # contains + search find it too
        st, listed = _call(
            customer,
            method="GET",
            query=[
                ("filter[0][key]", "name"),
                ("filter[0][op]", "contains"),
                ("filter[0][value]", marker[:12].upper()),  # ILIKE — case-insensitive
            ],
        )
        assert st == 200 and listed["extra"]["total"] == 1
        st, searched = _call(customer, method="GET", query=[("searchTerm", marker)])
        assert st == 200 and searched["extra"]["total"] == 1

        # update → effect: visible on a fresh read; untouched fields survive
        st, updated = _call(
            customer,
            method="PATCH",
            handle=rec_id,
            body=json.dumps({"email": "live@test.example"}).encode(),
        )
        assert st == 200, updated
        st, read = _call(customer, method="GET", handle=rec_id)
        assert read["data"]["email"] == "live@test.example"
        assert read["data"]["name"] == marker, "PATCH must not clobber other fields"

        # readOnly write is rejected with the field list
        st, rejected = _call(
            customer,
            method="PATCH",
            handle=rec_id,
            body=json.dumps({"id": "cus_1"}).encode(),
        )
        assert st == 409 and "id" in rejected["fields"]

        # tag action → effect on the record
        st, tagged = _call_action(customer, "addTag", rec_id, {"title": "live-tag"})
        assert st == 200, tagged
        st, read = _call(customer, method="GET", handle=rec_id)
        assert "live-tag" in (read["data"].get("tags") or [])
        st, _ = _call_action(customer, "removeTag", rec_id, {"title": "live-tag"})
        st, read = _call(customer, method="GET", handle=rec_id)
        assert "live-tag" not in (read["data"].get("tags") or [])

        # sort determinism: two identical calls return identical id order
        q = [("sort", "name"), ("page[size]", "50")]
        _, a = _call(customer, method="GET", query=list(q))
        _, b = _call(customer, method="GET", query=list(q))
        assert [r["id"] for r in a["data"]] == [r["id"] for r in b["data"]]
    finally:
        if rec_id:
            _sql(lambda pool: pool.execute("DELETE FROM neo_customer WHERE id = $1", rec_id))


def _call_action(adapter, key, rec_id, command):
    resp = asyncio.run(
        adapter.action(
            action_key=key,
            handle=rec_id,
            body=json.dumps({"ids": [rec_id], "command": command}).encode(),
            base_url="",
            token="",
        )
    )
    return resp.status_code, json.loads(resp.content or b"{}")


def test_unknown_field_write_is_409_and_leaves_no_row():
    victim = ADAPTERS["Tag"]  # its field is `label` — `title` is unknown
    before = _sql(lambda pool: pool.fetchval(f"SELECT count(*) FROM {victim._table}"))
    st, body = _call(victim, method="POST", body=json.dumps({"title": "x"}).encode())
    assert st == 409 and "title" in body["fields"], body
    after = _sql(lambda pool: pool.fetchval(f"SELECT count(*) FROM {victim._table}"))
    assert before == after


def test_create_and_delete_roundtrip():
    """Standalone opens delete — prove the full lifecycle on Tag."""
    tag = ADAPTERS["Tag"]
    st, created = _call(tag, method="POST", body=json.dumps({"label": "live-tmp"}).encode())
    assert st == 201, created
    rec_id = created["data"]["id"]
    st, _ = _call(tag, method="DELETE", handle=rec_id)
    assert st == 200
    st, _ = _call(tag, method="GET", handle=rec_id)
    assert st == 404


def test_search_answers_with_total():
    customer = ADAPTERS["Customer"]
    st, body = _call(customer, method="GET", query=[("searchTerm", "zzz-nope-nothing")])
    assert st == 200 and body["extra"]["total"] == 0


def test_record_tags_upsert_tag_master_rows():
    """A tag used on any record exists as a Tag master row afterwards — the
    facade's upstream behavior ('created automatically if new')."""
    customer = ADAPTERS["Customer"]
    tag_entity = ADAPTERS["Tag"]
    marker = f"upsert-{uuid.uuid4().hex[:8]}"
    rec_id = None
    try:
        # via create
        st, created = _call(
            customer, method="POST",
            body=json.dumps({"name": f"tag-upsert-{marker}", "tags": [marker]}).encode(),
        )
        assert st == 201, created
        rec_id = created["data"]["id"]
        st, tags = _call(tag_entity, method="GET", query=[
            ("filter[0][key]", "label"), ("filter[0][op]", "equals"), ("filter[0][value]", marker),
        ])
        assert st == 200 and tags["extra"]["total"] == 1, tags

        # via addTag action — and no duplicate for the existing title
        st, _ = _call_action(customer, "addTag", rec_id, {"title": marker + "-b"})
        assert st == 200
        st, tags = _call(tag_entity, method="GET", query=[
            ("filter[0][key]", "label"), ("filter[0][op]", "contains"), ("filter[0][value]", marker),
        ])
        assert st == 200 and tags["extra"]["total"] == 2, tags

        # re-writing the same tags creates nothing new
        st, _ = _call(customer, method="PATCH", handle=rec_id,
                      body=json.dumps({"tags": [marker, marker + "-b"]}).encode())
        assert st == 200
        st, tags = _call(tag_entity, method="GET", query=[
            ("filter[0][key]", "label"), ("filter[0][op]", "contains"), ("filter[0][value]", marker),
        ])
        assert tags["extra"]["total"] == 2
    finally:
        def _cleanup(pool):
            async def go():
                if rec_id:
                    await pool.execute("DELETE FROM neo_customer WHERE id = $1", rec_id)
                await pool.execute("DELETE FROM neo_tag WHERE data->>'label' LIKE $1", f"upsert-%")
            return go()
        _sql(_cleanup)
