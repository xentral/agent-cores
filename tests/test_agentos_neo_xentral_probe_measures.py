"""The probe must measure the claim it records, not the HTTP status it got.

Every verdict in the committed manifest was `pass` before this: `read` stamped on
every declared path before a payload was looked at, `filter`/`sort` on a 200 with
the rows never inspected, and actions green on a 4xx that proved only that a route
exists. The vocabulary (`verdicts.py`) gave those weak results their own words;
this file covers the measurements that decide between them.

Nothing here talks to a tenant — the probes are driven against canned payloads.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.checks import verify
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN

# ---- observing a read ----------------------------------------------------


def test_a_value_in_any_record_counts_as_observed() -> None:
    records = [{"name": None}, {"name": "Zelt"}]
    assert verify._observed(records, "name")


def test_a_path_no_record_fills_is_not_observed() -> None:
    """The 1218-verdict case: declared by the schema, never supplied by upstream."""
    assert not verify._observed([{"name": None}, {"name": ""}], "name")


def test_false_and_zero_are_values() -> None:
    """A merchant set them. Only absence is nothing — this is not a truth test."""
    assert verify._observed([{"flag": False}], "flag")
    assert verify._observed([{"count": 0}], "count")


def test_a_line_item_field_is_reached_through_the_collection() -> None:
    """`_value_at` stops at the first list, so no line-item field could ever be
    observed — every one of them would read `unobserved` forever."""
    records = [{"items": [{"description": None}, {"description": "Zeltstange"}]}]
    assert verify._observed(records, "items.description")


def test_an_empty_collection_observes_nothing_below_it() -> None:
    assert not verify._observed([{"items": []}], "items.description")


# ---- did the sort actually order the page? -------------------------------


def _page(values: list[Any], path: str = "number") -> dict[str, Any]:
    return {"data": [{path: v, "id": f"id{i}"} for i, v in enumerate(values)]}


def test_a_page_ordered_both_ways_is_proof() -> None:
    verdict, note = verify._sort_effect("number", _page([3, 2, 1]), _page([1, 2, 3]), 200)
    assert verdict == PROVEN
    assert note is None


def test_an_ignored_sort_key_is_a_failure_not_a_weak_pass() -> None:
    """The failure the old status check could not see: upstream answers 200 and
    returns its default order, which reads as a working sort."""
    same = _page([2, 1, 3])
    verdict, note = verify._sort_effect("number", same, _page([2, 1, 3]), 200)
    assert verdict == "fail"
    assert "same order" in note


def test_too_few_distinct_values_is_not_assertable() -> None:
    """Neither proof nor defect — the page simply cannot answer the question."""
    verdict, note = verify._sort_effect("number", _page([7, 7, 7]), _page([7, 7, 7]), 200)
    assert verdict == "accepted"
    assert "distinct" in note


def test_a_reference_sorted_by_a_column_we_cannot_read_stays_weak() -> None:
    """Upstream may order by the referent's name while the row carries only an id.
    That is a real capability we cannot assert — it must not go red."""
    desc = _page([{"id": "b"}, {"id": "a"}, {"id": "c"}], "project")
    asc = _page([{"id": "c"}, {"id": "a"}, {"id": "b"}], "project")
    verdict, _ = verify._sort_effect("project", desc, asc, 200)
    assert verdict == "accepted"


def test_a_failed_ascending_leg_does_not_claim_proof() -> None:
    verdict, note = verify._sort_effect("number", _page([3, 2, 1]), {}, 500)
    assert verdict == "accepted"
    assert "500" in note


# ---- what an action's answer is worth ------------------------------------


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()


class _Adapter:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def action(self, **_: Any) -> _Resp:
        return self._resp


def _probe(status: int, payload: Any = None) -> tuple[str | None, str | None]:
    return asyncio.run(
        verify._probe_action(_Adapter(_Resp(status, payload)), "send", "id1", "https://x", "t")
    )


@pytest.mark.parametrize("status", [200, 202, 204])
def test_a_2xx_is_executed_not_proven(status: int) -> None:
    """Data changed, but this probe never read the change back. Only a net-zero
    round-trip (the tag actions) earns `pass`."""
    verdict, note = _probe(status)
    assert verdict == "executed"
    assert "not read back" in note


@pytest.mark.parametrize("status", [400, 409, 422])
def test_a_refused_probe_proves_the_route_and_nothing_else(status: int) -> None:
    """34 of 62 committed action verdicts were green on exactly this."""
    verdict, note = _probe(status)
    assert verdict == "reachable"
    assert "reachable" in note


def test_a_refusal_from_our_own_validator_says_so() -> None:
    """Every 422 in the committed file came from the core's input validation, before
    the upstream was ever called — that proves even less than an upstream refusal,
    and the note must not let the two look alike."""
    _, note = _probe(422, {"title": "sendEmail requires command.to", "source": "core"})
    assert "the core's own validator" in note
    _, upstream_note = _probe(422, {"title": "nope"})
    assert "upstream" in upstream_note


@pytest.mark.parametrize("status", [404, 405, 501, 500, 502])
def test_a_missing_route_fails(status: int) -> None:
    assert _probe(status)[0] == "fail"


def test_a_declared_wish_is_left_alone() -> None:
    """It stays blue: not measured, not a failure."""
    assert _probe(409, {"wish": "no upstream endpoint yet"}) == (None, None)


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_failure_aborts_instead_of_being_recorded(status: int) -> None:
    """The one that used to grade `pass` as "reachable — 401": an expired token
    painted whole action suites green. Grading it `fail` would be as wrong in the
    other direction — 62 capability-shaped verdicts that really say "not logged in",
    which the next reader would retire wishes over. So it is not a verdict at all."""
    with pytest.raises(verify._AuthFailed):
        _probe(status)


# ---- the whole field pass, driven against canned payloads ----------------


class _Manifest:
    key = "Thing"
    operations = ("list", "read")


class _StubAdapter:
    """Enough adapter to drive `_verify_entity`: a schema, a search contract, and a
    `request` that answers from canned data. Writes are not declared, so the update
    and create passes stay out of the way."""

    manifest = _Manifest()
    detail_only_sections = ()

    def __init__(
        self, rows: list[dict[str, Any]], *, filter_hits: list[dict] | None = None
    ) -> None:
        self.rows = rows
        self.filter_hits = rows[:1] if filter_hits is None else filter_hits
        self.seen: list[list[tuple[str, str]]] = []

    def fields(self) -> dict[str, Any]:
        return {
            "id": {"type": "string"},
            "name": {"type": "string", "filterable": True, "sortable": True, "searchable": True},
            "note": {"type": "string"},
            "city": {"type": "string", "searchable": True},
            "items": {"node": {"properties": {"description": {"type": "string"}}}},
        }

    def search_fields(self) -> tuple[str, ...]:
        # `city` is declared searchable but absent here — the nested-leaf gap.
        return ("name",)

    async def request(self, *, method, handle, query, body, base_url, token, **_):
        self.seen.append(list(query))
        q = dict(query)
        if handle:
            return _Resp(200, {"data": self.rows[0]})
        if q.get("filter[0][key]") == "search":
            return _Resp(200, {"data": self.filter_hits})
        if q.get("filter[0][key]") == "name":
            return _Resp(200, {"data": self.filter_hits})
        if "sort" in q:
            ordered = sorted(self.rows, key=lambda r: r["name"], reverse=q["sort"].startswith("-"))
            return _Resp(200, {"data": ordered})
        return _Resp(200, {"data": self.rows})


ROWS = [
    {"id": "id0", "name": "Alpha", "note": None, "items": [{"description": "line"}]},
    {"id": "id1", "name": "Beta", "note": None, "items": []},
]


def _run(adapter: _StubAdapter) -> dict[str, Any]:
    result, _summary = asyncio.run(verify._verify_entity(adapter, "https://x", "t"))
    return result["fields"]


def test_a_declared_but_never_filled_field_is_unobserved_not_proven() -> None:
    fields = _run(_StubAdapter(ROWS))
    assert fields["name"]["read"] == PROVEN
    assert fields["note"]["read"] == "unobserved"
    assert "nothing proven either way" in fields["note"]["readNote"]


def test_a_line_item_field_is_observed_through_the_collection() -> None:
    assert _run(_StubAdapter(ROWS))["items.description"]["read"] == PROVEN


def test_a_filter_that_returns_its_own_record_is_proven() -> None:
    assert _run(_StubAdapter(ROWS))["name"]["filter"] == PROVEN


def test_a_filter_the_upstream_ignored_is_only_accepted() -> None:
    """200 with rows that do not include the record the value came from — the
    "answers 200 with the unfiltered collection" trap, which a status check grades
    green."""
    fields = _run(_StubAdapter(ROWS, filter_hits=[{"id": "other", "name": "Gamma"}]))
    assert fields["name"]["filter"] == "accepted"
    assert "may have been ignored" in fields["name"]["filterNote"]


def test_search_goes_through_the_facade_contract() -> None:
    adapter = _StubAdapter(ROWS)
    _run(adapter)
    assert any(("filter[0][key]", "search") in q for q in adapter.seen), (
        "the probe must send the filter group the facade parses, not a bare ?search="
    )
    assert not any(any(k == "search" for k, _ in q) for q in adapter.seen)


def test_a_field_outside_the_fan_out_is_a_failure() -> None:
    """`city` is flagged searchable but `search_fields()` never reaches it, so a
    consolidated search cannot match on it. That is a real gap, not a weak result."""
    fields = _run(_StubAdapter(ROWS))
    assert fields["city"]["search"] == "fail"
    assert "fan-out" in fields["city"]["searchNote"]


# ---- the one action whose effect is free to check ------------------------


class _PdfAdapter:
    def __init__(self, status: int, body: bytes) -> None:
        self._status, self._body = status, body

    async def action(self, **_: Any) -> Any:
        class R:
            status_code = self._status
            content = self._body

        return R()


def _pdf(status: int, body: bytes) -> tuple[str | None, str | None]:
    return asyncio.run(
        verify._probe_download_pdf(_PdfAdapter(status, body), "id1", "https://x", "t")
    )


def test_a_rendered_pdf_is_real_proof() -> None:
    """Read-only and net-zero, so unlike `send` this one can reach `pass`."""
    verdict, note = _pdf(200, b"%PDF-1.7\n...")
    assert verdict == PROVEN
    assert "real PDF" in note


def test_a_200_that_is_not_a_pdf_is_a_failure() -> None:
    """The generic probe would grade this `executed` — the route answered, so it
    looks like it worked."""
    verdict, note = _pdf(200, b'{"title":"no template configured"}')
    assert verdict == "fail"
    assert "not a PDF" in note
