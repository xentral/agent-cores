"""The run must say whether the CRUD operations themselves work.

Every consumer of `verified.json` has read `entities.<key>.operations` since the
file existed — the backend reader documents the shape, `validate_cores.py` walks
it, the app renders it as the "Browse all / Open a single record / Create / Edit /
Delete" cards. This core's run never wrote the block, so all five showed *untested*
on entities whose field suite had just exercised them hundreds of times. (The older
`xentral_api` core writes it; the newer one silently did not.)

The verdicts are DERIVED from measurements the run makes anyway, never from what
the adapter declares — that is the whole distinction the file exists to keep.

Nothing here talks to a tenant.
"""

from __future__ import annotations

import asyncio
import collections
import json
from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.checks import verify
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()


ROWS = [
    {"id": "id0", "name": "Alpha", "note": "n", "city": "Köln"},
    {"id": "id1", "name": "Beta", "note": None, "city": None},
]


class _Adapter:
    """A read-only entity: enough to drive `_verify_entity` without the write
    suites. `single` pins what the GET /<id> answers."""

    detail_only_sections = ()

    def __init__(self, ops: tuple[str, ...] = ("list", "read"), single: Any = None) -> None:
        self.manifest = type("M", (), {"key": "Thing", "operations": ops})()
        self.single = single
        self.handles: list[str] = []

    def fields(self) -> dict[str, Any]:
        return {"id": {"type": "string"}, "name": {"type": "string"}, "city": {"type": "string"}}

    def search_fields(self) -> tuple[str, ...]:
        return ()

    async def request(self, *, method, handle, query, body, base_url, token, **_):
        if handle:
            self.handles.append(handle)
            return self.single if self.single is not None else _Resp(200, {"data": ROWS[0]})
        return _Resp(200, {"data": ROWS})


def _ops(adapter: _Adapter) -> dict[str, str]:
    result, _summary = asyncio.run(verify._verify_entity(adapter, "https://x", "t"))
    return (result or {}).get("operations") or {}


# ---- list and read -------------------------------------------------------


def test_arriving_at_all_proves_the_list() -> None:
    """The function returns early unless the list read answered 200 with rows, so by
    the time verdicts are recorded `list` is already demonstrated."""
    assert _ops(_Adapter())["list"] == PROVEN


def test_the_single_read_must_return_the_record_it_was_asked_for() -> None:
    adapter = _Adapter()
    assert _ops(adapter)["read"] == PROVEN
    assert adapter.handles == ["id0"], "the richest sampled record is the one read back"


def test_a_200_carrying_someone_else_is_not_a_working_single_read() -> None:
    """Answered, but not demonstrably with the requested record. Neither proof nor a
    broken route — exactly what `accepted` is for."""
    assert _ops(_Adapter(single=_Resp(200, {"data": {"id": "somebody-else"}})))["read"] == (
        "accepted"
    )


def test_a_404_on_the_single_read_is_a_failure() -> None:
    assert _ops(_Adapter(single=_Resp(404, {"title": "gone"})))["read"] == "fail"


def test_an_unauthenticated_single_read_aborts_the_run() -> None:
    """A 401 anywhere is a broken run, not a property of the capability — recording
    it as `fail` would claim upstream is broken when the token merely expired."""
    with pytest.raises(verify._AuthFailed):
        _ops(_Adapter(single=_Resp(401, {"title": "expired"})))


# ---- only what the entity declares --------------------------------------


def test_an_operation_the_entity_does_not_offer_is_never_recorded() -> None:
    """A read-only entity must not sprout create/update/delete verdicts, and the
    single read must not be attempted when `read` is not on offer."""
    adapter = _Adapter(ops=("list",))
    ops = _ops(adapter)
    assert set(ops) == {"list"}
    assert adapter.handles == []


# ---- create / update, from the field suite's counters -------------------


def _counter(**kw: int) -> collections.Counter[str]:
    return collections.Counter(kw)


def test_one_field_that_persisted_proves_the_operation() -> None:
    """The operation asks whether the entity can be written at all — a create that
    lands 30 of 31 fields is a working create."""
    assert verify._operation_from_counts(_counter(**{PROVEN: 1, "fail": 30})) == PROVEN


def test_attempted_and_only_ever_failed_is_a_failed_operation() -> None:
    assert verify._operation_from_counts(_counter(fail=3)) == "fail"


def test_attempted_without_a_single_assertion_is_not_proof() -> None:
    """`accepted` means the request went through and nothing was checked. Reading
    that as green is the exact failure the verdict vocabulary was introduced for."""
    assert verify._operation_from_counts(_counter(accepted=5)) == "accepted"


def test_nothing_attempted_stays_absent() -> None:
    """Absent is untested, and untested must never be manufactured into a verdict."""
    assert verify._operation_from_counts(_counter()) is None


# ---- delete, from the create probes' cleanup ----------------------------


def test_a_clean_cleanup_proves_delete() -> None:
    assert verify._delete_verdict(None, 204) == PROVEN


def test_one_failed_cleanup_outranks_the_successes_around_it() -> None:
    """Worst answer wins in both directions: a later success must not overwrite an
    earlier failure, and an earlier success must not soften a later one."""
    assert verify._delete_verdict(PROVEN, 500) == "fail"
    assert verify._delete_verdict("fail", 204) == "fail"
