"""`release` commits, so probing it is a question of what the probe is allowed to ruin.

It takes no input, which means the generic empty-command probe is NOT refused at
validation the way `send` or `splitOrder` are — it runs. That the run has never
actually released anything is luck: the sampled documents are already released and
upstream answers 409 on their state. Sample a draft and the identical call releases
it and takes a number from the range, with `executed` as the only trace.

Effect-checking it cannot be a round trip either. Measured on mvp: create → `draft`,
release → status `sent` AND number `2026-30005`, cancel → `cancelled`, delete → 409
(only drafts are deletable). The document stays. So the probe confines the cost to a
document it made itself, and cancels it afterwards.

Nothing here talks to a tenant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.checks import verify
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()


class _Doc:
    """A document that models the real lifecycle: created as a draft with no number,
    `release` moves it off draft AND assigns one, `cancel` flips it to cancelled."""

    manifest = type("M", (), {"key": "Quote", "operations": ("list", "read", "create", "delete")})()

    def __init__(
        self,
        *,
        refuse: dict[str, int] | None = None,
        deaf: bool = False,
        reject_partners: set[str] | None = None,
        number_only: bool = False,
    ) -> None:
        self.number_only = number_only
        self.refuse = refuse or {}
        self.deaf = deaf
        self.reject = reject_partners or set()
        self.status = "draft"
        self.number = None
        self.born_numbered = False
        self.calls: list[str] = []
        self.deleted = False

    async def request(self, *, method, handle, query, body, base_url, token, **_):
        if method == "POST":
            self.calls.append("POST")
            # `_Probe.req` hands the adapter ENCODED json, not a dict — decoding it
            # here is what makes this double behave like the real request path.
            sent = json.loads(body) if body else {}
            pid = (sent.get("customer") or {}).get("id")
            if pid in self.reject:
                return _Resp(400, {"title": "The specified address does not exist."})
            born = "2026-00001" if self.born_numbered else None
            return _Resp(201, {"data": {"id": "quo_1", "status": self.status, "number": born}})
        if method == "DELETE":
            self.calls.append("DELETE")
            self.deleted = True
            return _Resp(204)
        return _Resp(200, {"data": {"id": "quo_1", "status": self.status, "number": self.number}})

    async def action(self, *, action_key: str, **_: Any) -> _Resp:
        self.calls.append(action_key)
        if action_key in self.refuse:
            return _Resp(self.refuse[action_key], {"title": "nope"})
        if not self.deaf:
            if action_key == "release":
                self.number = "2026-30005"
                if not self.number_only:
                    self.status = "sent"
            elif action_key == "cancel":
                self.status = "cancelled"
        return _Resp(200, {"data": {"id": "quo_1"}})


DRAFT_BODY = {"note": "verify release probe"}


def _probe(doc: _Doc) -> dict[str, tuple[str, str]]:
    return asyncio.run(
        verify._probe_release_cancel(doc, DRAFT_BODY, "customer", ["cus_5"], "https://x", "t")
    )


# ---- the effect probe ----------------------------------------------------


def test_release_needs_both_traces_and_takes_cancel_with_it() -> None:
    doc = _Doc()
    out = _probe(doc)
    assert out["release"][0] == PROVEN
    assert "2026-30005" in out["release"][1], "the number is half the proof"
    assert out["cancel"][0] == PROVEN
    assert doc.calls == ["POST", "release", "cancel"]
    assert doc.status == "cancelled", "the probe must not leave a live document"


def test_a_200_that_moves_nothing_is_a_failure() -> None:
    """The reason the verdict is read off the record. An action that answers 200 and
    leaves the document in `draft` is exactly what `executed` used to paint green."""
    doc = _Doc(deaf=True)
    out = _probe(doc)
    assert out["release"][0] == "fail"
    assert "accepted without effect" in out["release"][1]
    assert "cancel" not in out, "nothing was released, so there is nothing to cancel"


def test_a_refused_release_discards_its_own_draft() -> None:
    """It never left `draft`, so it is still deletable — and must be deleted, or the
    probe litters the tenant with drafts every run."""
    doc = _Doc(refuse={"release": 409})
    out = _probe(doc)
    assert out["release"][0] == "fail"
    assert doc.deleted is True
    assert "cancel" not in out


def test_a_failed_cancel_names_the_document_it_left_released() -> None:
    """The one outcome that leaves a LIVE document. A released quote nobody can find
    again is worse than a red cell."""
    doc = _Doc(refuse={"cancel": 500})
    out = _probe(doc)
    assert out["release"][0] == PROVEN
    assert out["cancel"][0] == "fail"
    assert "quo_1" in out["cancel"][1] and "2026-30005" in out["cancel"][1]
    assert "LEFT RELEASED" in out["cancel"][1]


def test_a_record_that_already_has_a_number_is_put_back_untouched() -> None:
    """The guard is the NUMBER, not the word `draft` — measured on mvp, a fresh Return
    is created as `requested`, so a status list would have excluded it by mistake. A
    record that already carries a number is in the books and must not be touched."""
    doc = _Doc()
    doc.born_numbered = True
    assert _probe(doc) == {}
    assert doc.calls == ["POST", "DELETE"]


def test_a_document_that_starts_outside_draft_is_still_probed() -> None:
    """A Return is born `requested`, not `draft`."""
    doc = _Doc()
    doc.status = "requested"
    out = _probe(doc)
    assert out["release"][0] == PROVEN


def test_the_number_alone_proves_it_even_when_the_status_stands_still() -> None:
    """Measured on mvp: a Return answers release with 200, keeps status `requested`
    and takes number 500077. Requiring a status change too graded that a failure —
    and the failure path then tried to DELETE a document upstream had just numbered,
    which is how a released return came to be left behind."""
    doc = _Doc(number_only=True)
    doc.status = "requested"
    out = _probe(doc)
    assert out["release"][0] == PROVEN
    assert "stayed 'requested'" in out["release"][1]
    assert doc.calls == ["POST", "release", "cancel"], "it must be cancelled, not deleted"


def test_an_unusable_partner_does_not_end_the_probe() -> None:
    """mvp's first SalesOrder customer carries an address upstream then rejects it.
    The create probe walks several partners; so must this one, or the probe reports a
    capability gap that is really a bad sample."""
    doc = _Doc(reject_partners={"cus_bad"})
    out = asyncio.run(
        verify._probe_release_cancel(
            doc, DRAFT_BODY, "customer", ["cus_bad", "cus_5"], "https://x", "t"
        )
    )
    assert out["release"][0] == PROVEN
    assert doc.calls.count("POST") == 2


# ---- the guard on the generic probe --------------------------------------


def _fallback(sample: dict[str, Any], doc: _Doc) -> tuple[str | None, str | None]:
    return asyncio.run(
        verify._probe_release_fallback(doc, "release", "quo_9", sample, "https://x", "t")
    )


def test_a_draft_sample_is_never_generically_probed() -> None:
    """THE hazard. The generic probe would release a real document and burn a number
    from the range, and the file would say `executed` about it."""
    doc = _Doc()
    assert _fallback({"id": "quo_9", "status": "draft"}, doc) == (None, None)
    assert doc.calls == [], "not one request may be sent at a draft"


def test_a_released_sample_still_earns_its_reachability() -> None:
    """Upstream refuses on state, which says the route exists and nothing more —
    a real verdict that should not be thrown away with the hazard."""
    doc = _Doc(refuse={"release": 409})
    verdict, note = _fallback({"id": "quo_9", "status": "sent"}, doc)
    assert verdict == "reachable"
    assert doc.calls == ["release"]
