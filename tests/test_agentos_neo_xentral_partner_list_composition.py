"""A partner collection is answered complete or null — never as a fragment.

The shipping addresses and contacts live in sub-resources, so composing them
costs two upstream calls PER ROW. List pages beyond ``_COMPOSE_LIST_LIMIT``
therefore do not compose — and used to answer the fragment map_read had for
free: the main address plus the billing singleton.

That fragment is indistinguishable from a complete set, and ``addresses`` is a
FULL DESIRED SET on write. Studio's mobile entity view PATCHes a whole list row
on any edit (StudioEntityMobile.js seeds every editable field from the row and
sends it without diffing), so renaming a customer there deleted all of its
shipping addresses. ``contacts`` was accidentally safe the whole time — it
already answered ``null``, and the sync skips ``None``.

So: null is the not-loaded marker for BOTH collections, on both sides. Reading
an uncomposed row answers null, and writing null leaves the collection alone.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter

_BASE = "/api/v3/suppliers"


class _Upstream:
    """Stateful fake Xentral holding `count` suppliers, each with one contact
    person and one shipping address in their own stores."""

    def __init__(self, count: int, *, shipping_fails: bool = False) -> None:
        self.shipping_fails = shipping_fails
        self.records = {
            str(i): {
                "id": i,
                "number": f"7005{i}",
                "primaryAddress": {
                    "name": f"Lieferant {i}",
                    "street": "Hafenstrasse 12",
                    "zipCode": "20457",
                    "city": "Hamburg",
                    "country": "DE",
                },
                "communication": {"language": "DE"},
                "tags": [],
            }
            for i in range(1, count + 1)
        }
        self.contacts = {
            str(i): {str(i): {"id": int(f"7{i}"), "type": "mrs", "name": f"Kontakt {i}"}}
            for i in range(1, count + 1)
        }
        self.shipping = {
            str(i): {str(i): {"id": int(f"4{i}"), "name": f"Lager {i}", "city": "Kiel"}}
            for i in range(1, count + 1)
        }
        self.sub_calls = 0

    def _store(self, sub: str, owner: str) -> dict[str, dict[str, Any]]:
        return (self.contacts if sub == "contactPersons" else self.shipping)[owner]

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}

        if path == _BASE and method == "GET":
            return httpx.Response(
                200,
                json={"data": list(self.records.values()), "meta": {"total": len(self.records)}},
            )
        rest = path[len(_BASE) + 1 :] if path.startswith(_BASE + "/") else ""
        parts = rest.split("/") if rest else []
        if len(parts) == 1 and parts[0] in self.records:
            if method == "GET":
                return httpx.Response(200, json={"data": self.records[parts[0]]})
            if method == "PATCH":
                for key, value in body.items():
                    if key in ("primaryAddress", "communication", "financials") and isinstance(
                        value, dict
                    ):
                        self.records[parts[0]].setdefault(key, {}).update(value)
                    else:
                        self.records[parts[0]][key] = value
                return httpx.Response(200, json={"data": self.records[parts[0]]})
        if len(parts) >= 2 and parts[0] in self.records:
            owner, sub = parts[0], parts[1]
            self.sub_calls += 1
            if sub == "deliveryAddresses" and self.shipping_fails:
                return httpx.Response(503, json={"title": "delivery address store unavailable"})
            store = self._store(sub, owner)
            if len(parts) == 2:
                if method == "GET":
                    return httpx.Response(200, json={"data": list(store.values())})
                if method == "POST":
                    store["new"] = {**body, "id": 999}
                    return httpx.Response(201, json={"data": store["new"]})
            if len(parts) == 3:
                if method == "PATCH":
                    return httpx.Response(200, json={"data": {}})
                if method == "DELETE":
                    store.pop(
                        next((k for k, v in store.items() if str(v.get("id")) == parts[2]), ""),
                        None,
                    )
                    return httpx.Response(204)

        raise AssertionError(f"unexpected call: {method} {path}")


def _run(up: _Upstream, factory) -> Any:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await factory(SupplierAdapter(), client)

    return asyncio.run(go())


def _list(up: _Upstream) -> list[dict[str, Any]]:
    resp = _run(
        up,
        lambda adapter, client: adapter.request(
            method="GET",
            handle=None,
            query=[("page[size]", "25")],
            body=None,
            base_url="https://unit.test",
            token="t",  # noqa: S106
            client=client,
        ),
    )
    assert resp.status_code == 200, resp.content
    return json.loads(resp.content)["data"]


def _get(up: _Upstream, handle: str) -> dict[str, Any]:
    resp = _run(
        up,
        lambda adapter, client: adapter.request(
            method="GET",
            handle=handle,
            query=[],
            body=None,
            base_url="https://unit.test",
            token="t",  # noqa: S106
            client=client,
        ),
    )
    assert resp.status_code == 200, resp.content
    return json.loads(resp.content)["data"]


def _update(up: _Upstream, handle: str, model: dict[str, Any]) -> httpx.Response:
    return _run(
        up,
        lambda adapter, client: adapter.request(
            method="PATCH",
            handle=handle,
            query=[],
            body=json.dumps(model).encode(),
            base_url="https://unit.test",
            token="t",  # noqa: S106
            client=client,
        ),
    )


# ---- reads ---------------------------------------------------------------


def test_small_page_is_composed_completely():
    """Tiny pages still pay the N+1 and answer the whole thing."""
    rows = _list(_Upstream(3))
    assert [r["contacts"][0]["name"] for r in rows] == ["Kontakt 1", "Kontakt 2", "Kontakt 3"]
    for row in rows:
        assert [a["type"] for a in row["addresses"]] == ["both", "shipping"]


def test_big_page_answers_null_instead_of_a_fragment():
    """THE regression. Beyond the compose limit the sub-resources are unknown, so
    both collections must say null — not the main+billing fragment that reads as
    a complete set."""
    up = _Upstream(5)
    rows = _list(up)
    assert len(rows) == 5
    assert all(r["addresses"] is None for r in rows), "a truncated address list is a loaded gun"
    assert all(r["contacts"] is None for r in rows)
    assert up.sub_calls == 0, "the cheap path must stay cheap"


def test_detail_read_answers_null_when_the_shipping_store_is_unreachable():
    """Same rule when the sub-call FAILS: unknown is null, never main-only."""
    row = _get(_Upstream(1, shipping_fails=True), "sup_1")
    assert row["addresses"] is None
    assert row["contacts"][0]["name"] == "Kontakt 1"  # that store did answer


# ---- writing the marker back --------------------------------------------


def test_sending_a_list_row_back_deletes_nothing():
    """The Studio-mobile shape: the whole row goes back on any edit. With
    addresses/contacts null that must leave both stores untouched — and the real
    edit must still land."""
    up = _Upstream(5)
    row = _list(up)[0]
    # Studio seeds its form from the writable schema fields and sends them all
    # back undiffed — both collections included, and both null off a big page.
    payload = {
        "name": "Umbenannt GmbH",
        "addresses": row["addresses"],
        "contacts": row["contacts"],
    }
    assert payload["addresses"] is None and payload["contacts"] is None

    resp = _update(up, row["id"], payload)

    assert resp.status_code == 200, resp.content
    assert list(up.shipping["1"]) == ["1"], "round-tripping a list row deleted a shipping address"
    assert list(up.contacts["1"]) == ["1"]
    assert up.records["1"]["primaryAddress"]["name"] == "Umbenannt GmbH"


def test_explicit_empty_list_still_clears_the_collection():
    """null means "not loaded"; [] means "I want none left". The marker must not
    swallow a real clear."""
    up = _Upstream(1)
    resp = _update(up, "sup_1", {"addresses": []})
    assert resp.status_code == 200, resp.content
    assert up.shipping["1"] == {}
