"""One-shot: restate the committed verdicts in the vocabulary of `verdicts.py`.

The old manifest had only `pass`/`fail`, so a probe that could measure a facet only
weakly still had to write `pass`. Flipping the vocabulary in code without touching
the data would leave 1810 overstated verdicts in place; re-measuring first is not an
option either, because a full live run takes hours and `VERIFY_ACTIONS` mails real
customers.

So the file is restated mechanically, to the weakest verdict the OLD evidence
actually supports. Nothing here upgrades anything — that is the whole point. Where
the old probe checked an effect, the verdict survives; where it checked an HTTP
status, it steps down.

The action verdicts need no guesswork at all: the probe already wrote its evidence
into the notes, and the three phrasings partition the set exactly (measured: 34
"reachable — ", 10 "EXECUTED", 18 tag round-trips, 62 total, no remainder).

Run once, review the diff, delete nothing — the script stays as the record of how
the numbers moved:

    PYTHONPATH=<agent-os>/backend uv run python scripts/migrate_verified_vocabulary.py
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from typing import Any

_MANIFEST = (
    Path(__file__).resolve().parent.parent / "cores" / "agentos_neo_xentral" / "verified.json"
)

# facet → what a `pass` in the old file really proved.
#
# `create`/`update` are absent because they keep their verdict: those probes wrote a
# value, read it back and compared it, which is exactly what `pass` now means.
_FIELD_DOWNGRADE = {
    # Stamped on every declared schema path before any payload was inspected, so it
    # never meant "upstream supplies this" — only "the schema declares it".
    "read": "unobserved",
    # HTTP 200 with the returned rows never checked against the filter value.
    "filter": "accepted",
    # HTTP 200 with the returned page never checked for order.
    "sort": "accepted",
}

# `search` is not downgraded but DROPPED: the old probe sent a bare `?search=` query
# param, which the facade's search never sees (it reads `filter[i][key]=search`), so
# the fan-out never ran and the parameter went to the upstream verbatim. The verdict
# is not weak evidence, it is evidence about something else entirely.
_SEARCH_DROP_NOTE = (
    "verdict dropped in the vocabulary migration: the probe sent a bare `search` "
    "query param, which never reaches the facade's search fan-out — it proved "
    "nothing about this field"
)

# note phrasing → what the action probe actually established.
_ACTION_FROM_NOTE = (
    ("reachable", "reachable"),  # route exists, refused our probe (4xx)
    ("EXECUTED", "executed"),  # 2xx, effect never read back
    ("net-zero", "pass"),  # addTag/removeTag: written, seen, undone
    ("effect verified", "pass"),
)


def _action_verdict(old: str, note: str) -> str:
    if old != "pass":
        return old
    for phrase, verdict in _ACTION_FROM_NOTE:
        if phrase in note:
            return verdict
    raise SystemExit(
        f"unclassifiable action verdict {old!r} with note {note!r} — the migration "
        "must read the evidence, not guess it"
    )


def main() -> int:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    counts: collections.Counter[str] = collections.Counter()

    for entity in (manifest.get("entities") or {}).values():
        for facets in (entity.get("fields") or {}).values():
            for facet, weaker in _FIELD_DOWNGRADE.items():
                if facets.get(facet) == "pass":
                    facets[facet] = weaker
                    counts[f"{facet}: pass → {weaker}"] += 1
            if facets.get("search") == "pass":
                del facets["search"]
                facets["searchNote"] = _SEARCH_DROP_NOTE
                counts["search: pass → dropped"] += 1

        for results_key, notes_key in (
            ("actions", "actionsNotes"),
            ("processSteps", "processStepsNotes"),
        ):
            results: dict[str, Any] = entity.get(results_key) or {}
            notes = entity.get(notes_key) or {}
            for key, old in list(results.items()):
                new = _action_verdict(old, notes.get(key) or "")
                if new != old:
                    counts[f"{results_key}: pass → {new}"] += 1
                results[key] = new

    # A reader can tell which vocabulary it is holding without inspecting values.
    manifest["schemaVersion"] = 2
    _MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )

    for label, n in sorted(counts.items()):
        print(f"{n:5d}  {label}")
    print(f"\nwrote {_MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
