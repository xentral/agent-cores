"""Partner collections ride along on the read as ``?include=``.

Verified on mvp: ``include=contactPersons,deliveryAddresses`` is honored on
/suppliers and /customers, the inline rows are byte-identical to what the
sub-resource endpoints return, and nothing is capped (seeded 30 contacts →
inline 30, paged 30). One 25-row page costs +60ms with both includes; the
per-row composition it replaces took ~11s for 51 calls.

So list pages answer real data instead of the not-loaded marker. What stays is
the honesty rule: a build that does not know the includes must not be served a
FRAGMENT. Xentral rejects the whole request for an unknown include, so the read
retries once without it, and the rows then answer null — never main+billing
dressed up as the complete set.

Three upstream behaviors are covered: honors the include, silently ignores it,
and rejects it with a 400.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter

_BASE = "/api/v3/suppliers"


class _Upstream:
    """Fake Xentral with a switch for how it treats ``include``."""

    def __init__(self, count: int, *, mode: str = "honors", contacts_per: int = 1) -> None:
        assert mode in ("honors", "ignores", "rejects")
        self.mode = mode
        self.records = {
            str(i): {
                "id": i,
                "number": f"7005{i}",
                "primaryAddress": {"name": f"Lieferant {i}", "city": "Hamburg"},
                "communication": {"language": "DE"},
                "tags": [],
            }
            for i in range(1, count + 1)
        }
        self.contacts = {
            str(i): [
                {"id": int(f"7{i}{n}"), "type": "mrs", "name": f"Kontakt {i}.{n}"}
                for n in range(contacts_per)
            ]
            for i in range(1, count + 1)
        }
        self.shipping = {
            str(i): [{"id": int(f"4{i}"), "name": f"Lager {i}", "city": "Kiel"}]
            for i in range(1, count + 1)
        }
        self.sub_calls = 0
        self.include_asks: list[str] = []

    def _decorate(self, rec: dict[str, Any], include: str) -> dict[str, Any]:
        if self.mode != "honors":
            return rec
        out = dict(rec)
        rid = str(rec["id"])
        if "contactPersons" in include:
            out["contactPersons"] = self.contacts[rid]
        if "deliveryAddresses" in include:
            out["deliveryAddresses"] = self.shipping[rid]
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901
        path, method = request.url.path, request.method
        include = request.url.params.get("include") or ""
        if include:
            self.include_asks.append(include)
        wants_sub = "contactPersons" in include or "deliveryAddresses" in include
        if wants_sub and self.mode == "rejects" and method == "GET":
            return httpx.Response(
                400,
                json={"message": "Requested include(s) `contactPersons` are not allowed."},
            )

        if path == _BASE and method == "GET":
            rows = [self._decorate(r, include) for r in self.records.values()]
            return httpx.Response(200, json={"data": rows, "meta": {"total": len(rows)}})

        rest = path[len(_BASE) + 1 :] if path.startswith(_BASE + "/") else ""
        parts = rest.split("/") if rest else []
        if len(parts) == 1 and parts[0] in self.records:
            if method == "GET":
                return httpx.Response(
                    200, json={"data": self._decorate(self.records[parts[0]], include)}
                )
            if method == "PATCH":
                body = json.loads(request.content) if request.content else {}
                for key, value in body.items():
                    if key in ("primaryAddress", "communication") and isinstance(value, dict):
                        self.records[parts[0]].setdefault(key, {}).update(value)
                    else:
                        self.records[parts[0]][key] = value
                return httpx.Response(200, json={"data": self.records[parts[0]]})
        if len(parts) >= 2 and parts[0] in self.records:
            self.sub_calls += 1
            owner, sub = parts[0], parts[1]
            store = self.contacts if sub == "contactPersons" else self.shipping
            if len(parts) == 2 and method == "GET":
                return httpx.Response(200, json={"data": store[owner]})
            if len(parts) == 3 and method == "DELETE":
                store[owner] = [x for x in store[owner] if str(x["id"]) != parts[2]]
                return httpx.Response(204)
            if len(parts) == 2 and method == "POST":
                return httpx.Response(201, json={"data": {"id": 999}})
        raise AssertionError(f"unexpected call: {method} {path}?include={include}")


def _req(up: _Upstream, **kw) -> Any:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await SupplierAdapter().request(
                base_url="https://unit.test",
                token="t",  # noqa: S106
                client=client,
                query=kw.pop("query", []),
                body=kw.pop("body", None),
                **kw,
            )

    resp = asyncio.run(go())
    assert resp.status_code < 400, resp.content
    return json.loads(resp.content)["data"]


def _list(up: _Upstream):
    return _req(up, method="GET", handle=None, query=[("page[size]", "25")])


# ---- the include does its job -------------------------------------------


def test_big_page_is_complete_without_a_single_sub_call():
    """The whole point: 25 rows answered completely for one upstream call."""
    up = _Upstream(9)
    rows = _list(up)
    assert len(rows) == 9
    assert up.sub_calls == 0
    for i, row in enumerate(rows, start=1):
        assert [c["name"] for c in row["contacts"]] == [f"Kontakt {i}.0"]
        assert [a["type"] for a in row["addresses"]] == ["both", "shipping"]


def test_detail_read_needs_no_sub_calls_either():
    up = _Upstream(1)
    row = _req(up, method="GET", handle="sup_1")
    assert up.sub_calls == 0
    assert row["contacts"][0]["name"] == "Kontakt 1.0"
    assert [a["id"] for a in row["addresses"]] == ["adr_main", "adr_s41"]


def test_empty_inline_collection_is_empty_not_unknown():
    """[] from the include is a COMPLETE answer — a partner with no contacts.
    Answering null there would make every write skip a real 'clear them all'."""
    up = _Upstream(9, contacts_per=0)
    rows = _list(up)
    assert all(r["contacts"] == [] for r in rows)
    assert up.sub_calls == 0


# ---- builds that lack the include ---------------------------------------


def test_a_build_that_rejects_the_include_still_serves_the_entity():
    """Xentral 400s the WHOLE request for an unknown include. Retry without it —
    losing the collections beats losing Customer and Supplier entirely."""
    up = _Upstream(9, mode="rejects")
    rows = _list(up)
    assert len(rows) == 9
    assert all(r["contacts"] is None and r["addresses"] is None for r in rows)
    assert any("contactPersons" in a for a in up.include_asks), "it must have tried"


def test_a_build_that_ignores_the_include_answers_null_not_a_fragment():
    """Silently dropped include: map_read still has the two singletons off the
    record. That fragment must not reach the caller as a full desired set."""
    up = _Upstream(9, mode="ignores")
    rows = _list(up)
    assert all(r["addresses"] is None for r in rows)
    assert all(r["contacts"] is None for r in rows)
    assert up.sub_calls == 0


def test_small_pages_and_details_fall_back_to_the_sub_calls():
    """Where the N+1 is affordable, an include-less build still gets complete
    answers — the old path survives as the fallback."""
    up = _Upstream(2, mode="ignores")
    rows = _list(up)
    assert up.sub_calls == 4  # 2 rows x (contacts + shipping)
    for i, row in enumerate(rows, start=1):
        assert [c["name"] for c in row["contacts"]] == [f"Kontakt {i}.0"]
        assert [a["type"] for a in row["addresses"]] == ["both", "shipping"]

    up = _Upstream(1, mode="rejects")
    row = _req(up, method="GET", handle="sup_1")
    assert up.sub_calls == 2
    assert row["contacts"][0]["name"] == "Kontakt 1.0"
    assert [a["type"] for a in row["addresses"]] == ["both", "shipping"]


# ---- writes -------------------------------------------------------------


def test_write_answer_rides_on_the_include_too():
    up = _Upstream(1)
    row = _req(
        up,
        method="PATCH",
        handle="sup_1",
        body=json.dumps({"website": "https://neu.example"}).encode(),
    )
    assert up.sub_calls == 0
    assert row["contacts"][0]["name"] == "Kontakt 1.0"
    assert [a["type"] for a in row["addresses"]] == ["both", "shipping"]


def test_collection_write_still_syncs_and_deletes():
    """The write contract is untouched by where the read data comes from."""
    up = _Upstream(1)
    _req(up, method="PATCH", handle="sup_1", body=json.dumps({"contacts": []}).encode())
    assert up.contacts["1"] == []


# ---- a build where the store does not exist at all ----------------------


class _NoShippingRoute(_Upstream):
    """gate56's shape: the include is silently ignored for this entity AND
    /suppliers/{id}/deliveryAddresses does not exist ("Route not found")."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("deliveryAddresses"):
            self.sub_calls += 1
            return httpx.Response(404, json={"error": {"message": "Route not found"}})
        return super().handler(request)


def test_a_missing_store_means_no_rows_not_unknown():
    """404 on the sub-resource is not a failure to read — the route is absent, so
    there are no rows of that kind and the singletons ARE the set. Answering null
    would hide the main address, which is known. Nor can the fragment bite: the
    same missing store makes the write-side delete impossible."""
    up = _NoShippingRoute(1, mode="ignores")
    row = _req(up, method="GET", handle="sup_1")
    assert [a["type"] for a in row["addresses"]] == ["both"]
    assert [c["name"] for c in row["contacts"]] == ["Kontakt 1.0"]


class _BrokenShippingStore(_Upstream):
    """The store EXISTS but errors — genuinely unknown."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("deliveryAddresses"):
            self.sub_calls += 1
            return httpx.Response(503, json={"title": "unavailable"})
        return super().handler(request)


def test_a_broken_store_still_answers_null():
    up = _BrokenShippingStore(1, mode="ignores")
    row = _req(up, method="GET", handle="sup_1")
    assert row["addresses"] is None
