"""The write-protection probe must be a round trip, not two independent shots.

`setWriteProtection` takes no input, so the generic empty-command probe does not get
refused at validation the way `send` or `splitOrder` do — it COMMITS. Probed the
generic way, the pair therefore earns `executed` (a verdict about an HTTP status) and
leaves the sampled document locked on a live tenant, because `removeWriteProtection`
is a separate target that may run on another record, later, or not at all.

So the two are probed together, against the state the document was already in, and
the verdict is read off the `writeProtection` FIELD — never off the action's echo, and
never off whether some other write succeeds: upstream's `writeProtectionBypassFields()`
lets `internebemerkung` and `status` through, so a successful note write on a locked
document is normal (see test_agentos_neo_xentral_write_protection.py).

Nothing here talks to a tenant — the probe is driven against canned payloads.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.checks import verify
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN

SET, REMOVE = "setWriteProtection", "removeWriteProtection"


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()


class _Doc:
    """A document that actually holds the flag, so the probe has something to read
    back. `refuse` pins an action to a status; `deaf` accepts the action with 200 and
    changes nothing — the silent no-op the field read exists to catch."""

    manifest = type("M", (), {"key": "Quote"})()

    def __init__(
        self, protected: bool, *, refuse: dict[str, int] | None = None, deaf: bool = False
    ):
        self.protected = protected
        self.refuse = refuse or {}
        self.deaf = deaf
        self.calls: list[str] = []

    async def action(self, *, action_key: str, **_: Any) -> _Resp:
        self.calls.append(action_key)
        if action_key in self.refuse:
            return _Resp(self.refuse[action_key], {"title": "nope"})
        if not self.deaf:
            self.protected = action_key == SET
        return _Resp(200, {"data": {"writeProtection": self.protected}})

    async def request(self, **_: Any) -> _Resp:
        return _Resp(200, {"data": {"id": "id1", "writeProtection": self.protected}})


class _Unreadable(_Doc):
    async def request(self, **_: Any) -> _Resp:
        return _Resp(200, {"data": {"id": "id1"}})


def _probe(doc: _Doc) -> dict[str, tuple[str, str]]:
    return asyncio.run(verify._probe_write_protection(doc, "id1", "https://x", "t"))


def test_an_unprotected_document_is_locked_and_unlocked_again() -> None:
    doc = _Doc(protected=False)
    out = _probe(doc)
    assert out[SET][0] == PROVEN and out[REMOVE][0] == PROVEN
    assert doc.calls == [SET, REMOVE]
    assert doc.protected is False, "the roundtrip must be net-zero"


def test_an_already_protected_document_is_probed_the_other_way_round() -> None:
    """Starting with `set` on a locked document proves nothing, and the `remove` that
    followed would unlock a document somebody had deliberately locked."""
    doc = _Doc(protected=True)
    out = _probe(doc)
    assert out[SET][0] == PROVEN and out[REMOVE][0] == PROVEN
    assert doc.calls == [REMOVE, SET]
    assert doc.protected is True, "the roundtrip must be net-zero"


def test_a_200_that_changes_nothing_is_a_failure_not_a_pass() -> None:
    """The whole reason the verdict is read off the field: an action that answers 200
    and leaves the flag alone is exactly what `executed` used to paint green."""
    doc = _Doc(protected=False, deaf=True)
    out = _probe(doc)
    assert out[SET][0] == "fail"
    assert "had no effect" in out[SET][1]
    assert REMOVE not in out, "nothing to say about the way back that was never reached"


def test_a_refused_first_flip_leaves_the_second_untested() -> None:
    doc = _Doc(protected=False, refuse={SET: 409})
    out = _probe(doc)
    assert out[SET][0] == "fail"
    assert REMOVE not in out
    assert doc.protected is False


def test_a_document_left_locked_says_so_by_id() -> None:
    """The one non-net-zero outcome. A locked document left behind silently is a
    support ticket, so the note must name the record and the state it is stuck in."""
    doc = _Doc(protected=False, refuse={REMOVE: 500})
    out = _probe(doc)
    assert out[SET][0] == PROVEN
    assert out[REMOVE][0] == "fail"
    assert "id1" in out[REMOVE][1] and "LEFT AT" in out[REMOVE][1]
    assert doc.protected is True


def test_an_unreadable_flag_probes_nothing_at_all() -> None:
    """No fallback to the generic probe: the fallback IS the hazard. Without a field
    to read back, a flip cannot be graded — and it would still commit."""
    doc = _Unreadable(protected=False)
    assert _probe(doc) == {}
    assert doc.calls == []
