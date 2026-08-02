"""The verdict vocabulary of ``verified.json`` — how strongly a capability was shown.

The manifest used to know three states: ``pass``, ``fail``, and absence (untested).
That left no room to record *how strongly* something was shown, so every probe that
could only make a weak claim had to either lie green or stay silent — and it lied
green. Measured on the committed file before this change: 1810 field verdicts, all
``pass``, not one ``fail``; 1218 of them ``read``, stamped on every declared schema
path before any payload was looked at; and 34 of 62 action verdicts green on nothing
but a 4xx proving the route exists.

That is not cosmetic. ``FacadeAdapterBase._proven`` retires a hand-written wish from
``priorities.json`` when a facet passes, so a weak verdict deletes a real backlog
entry. The old design's answer was a hand-maintained allowlist that excluded ``read``
— a rule living in one consumer's head while the other two kept rendering it green.

So the weak claims get their own words. ``pass`` is reserved for evidence that the
claim itself was asserted against real data:

============  =============================================================
``pass``      the claim was asserted — a value was observed, a write was read
              back, a filtered query returned the record it came from, an
              action's effect was seen
``accepted``  the request succeeded, but the claim could not be asserted
              (HTTP 200 with nothing checked beyond the status)
``unobserved``the probe ran and the sample gave nothing to assert against —
              a declared field no record carried a value for. **Not a
              failure**, and it must never render as one
``executed``  an action returned 2xx: data was changed, but not read back
``reachable`` the route or key demonstrably exists and refused our probe on
              validation or record state — no capability claim either way
``fail``      tested and found broken
*(absent)*    never tested
============  =============================================================

Why widen the enum rather than add a sibling ``<facet>Evidence`` field: every reader
of this data compares ``== "pass"``. New *values* therefore fall out of the green at
every existing reader by construction — fail-closed. A sibling field would be
fail-open, because a reader that ignores it keeps the old semantics. That is exactly
the failure mode being fixed.

Why keep the word ``pass`` instead of renaming it to ``proven``: a rename would make
an unmigrated reader show *everything* grey, discarding the 402 ``create``/``update``
verdicts that were always genuinely effect-checked.

Be clear about what that costs. ``scripts/validate_cores.py`` rejects any verdict
string outside this set, which catches a writer inventing a word or a reader and
writer drifting apart — but it CANNOT catch a probe that goes back to writing
``pass`` for a facet it only measured weakly, because that is the same string as a
legitimate proof. Nothing in the data can distinguish those. What protects the
meaning is that each probe passes what its success is worth (``verify.py::mark``'s
``when_ok``), and that is a code-review matter, not a schema one.
"""

from __future__ import annotations

#: The verdict that means "the capability itself was demonstrated".
PROVEN = "pass"

#: Every value a verdict may take on disk. ``scripts/validate_cores.py`` enforces it.
VERDICTS = frozenset(
    {
        PROVEN,
        "accepted",
        "unobserved",
        "executed",
        "reachable",
        "fail",
    }
)

#: Verdicts that are a deliberate, final answer rather than an open to-do. An action
#: that mails a customer can never be effect-checked on a live tenant, and a field the
#: instance never populates cannot be observed there — rendering those as "not yet
#: done" invites someone to go fix a thing that is not broken.
TERMINAL_BY_DESIGN = frozenset({"accepted", "unobserved", "executed", "reachable"})


def is_proven(verdict: object) -> bool:
    """Whether a verdict is evidence that the capability works.

    The single sanctioned test. Everything else — ``accepted``, ``unobserved``,
    ``executed``, ``reachable``, absence — is not proof, whatever the facet. Callers
    must not spell this out themselves; the point of the function is that there is
    one place to read the rule and one place to change it.
    """
    return verdict == PROVEN
