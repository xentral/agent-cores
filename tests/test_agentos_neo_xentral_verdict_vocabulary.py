"""Every verdict has exactly one meaning, and every reader knows all of them.

`verified.json` used to know only `pass`/`fail`/absent, so a probe that could
measure a facet only weakly still had to write `pass`. That is how 1218 `read`
verdicts came to be stamped before any payload was looked at, and how 34 of 62
action verdicts went green on a 4xx that proved nothing but the route's existence.

The fix is the vocabulary in `verdicts.py`. It only holds if two things stay true:
a weak verdict is never mistaken for proof (covered by the wish-hygiene tests, over
the whole enum), and no reader silently drops a verdict it has not been taught —
which is what the exporter tests below are for. Adding a word to the enum without
teaching the workbook must fail here rather than paint a blank cell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xentral_entity_cores.agentos_neo_xentral.verdicts import (
    PROVEN,
    TERMINAL_BY_DESIGN,
    VERDICTS,
    is_proven,
)

_MANIFEST = (
    Path(__file__).resolve().parent.parent / "cores" / "agentos_neo_xentral" / "verified.json"
)


def _exporter():
    """The workbook renderer, imported lazily — it needs openpyxl, which is supplied
    per call rather than being a repo dependency."""
    pytest.importorskip("openpyxl")
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "export_verified_xlsx.py"
    spec = importlib.util.spec_from_file_location("_export_verified_xlsx", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_only_proven_is_proof() -> None:
    assert is_proven(PROVEN)
    for verdict in VERDICTS - {PROVEN}:
        assert not is_proven(verdict), verdict


def test_absence_is_not_proof() -> None:
    """Untested is the absence of a verdict, and must not read as one."""
    assert not is_proven(None)
    assert not is_proven("")


def test_the_terminal_verdicts_are_weak_ones() -> None:
    """ "Terminal by design" says "do not file a bug", not "this works". Marking a
    proof or a failure terminal would silence the two verdicts that must be acted
    on."""
    assert TERMINAL_BY_DESIGN < VERDICTS
    assert PROVEN not in TERMINAL_BY_DESIGN
    assert "fail" not in TERMINAL_BY_DESIGN


@pytest.mark.parametrize("verdict", sorted(VERDICTS))
def test_the_workbook_renders_every_verdict_distinctly(verdict: str) -> None:
    """A verdict the exporter has not been taught would fall through to `offen`
    ("no run has looked at it") — turning a measured weak result back into an
    unmeasured one, which is the same class of lie in the other direction."""
    mod = _exporter()
    cell, _ = mod._cell({"verified": {"read": verdict}}, "read")
    expected = mod.OK if verdict == PROVEN else (mod.FAIL if verdict == "fail" else mod.WEAK)
    assert cell == expected
    assert cell in mod._FILL, f"{cell} has no fill — it would render unstyled"


@pytest.mark.parametrize("verdict", sorted(VERDICTS))
def test_the_workbook_renders_every_action_verdict_the_same_way(verdict: str) -> None:
    """Actions go through a second renderer. It must agree with the field one — they
    read the same vocabulary, and a disagreement is invisible in the finished
    sheet."""
    mod = _exporter()
    field_cell, _ = mod._cell({"verified": {"read": verdict}}, "read")
    action_cell, _ = mod._status({"verified": {"status": verdict}})
    assert action_cell == field_cell
    assert action_cell in mod._FILL


def test_an_action_keeps_the_note_that_says_how_strong_it_was() -> None:
    """Without it a `reachable` cell cannot be told from a real one — the note is
    the evidence, and it used to be dropped on the floor."""
    mod = _exporter()
    cell, note = mod._status(
        {"verified": {"status": "reachable", "note": "route exists; upstream refused (409)"}}
    )
    assert cell == mod.WEAK
    assert note == "route exists; upstream refused (409)"


def test_the_committed_manifest_speaks_the_vocabulary() -> None:
    """Mirrors the CI gate in `scripts/validate_cores.py`, so a bad verdict fails in
    the test run too rather than only at push time."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    bad: dict[str, set[str]] = {}
    for key, entity in (manifest.get("entities") or {}).items():
        found = set()
        for facets in (entity.get("fields") or {}).values():
            found |= {v for k, v in facets.items() if not k.endswith("Note")}
        for group in ("actions", "processSteps"):
            found |= set((entity.get(group) or {}).values())
        if found - VERDICTS:
            bad[key] = found - VERDICTS
    assert bad == {}, bad


def test_the_migrated_manifest_claims_no_unearned_proof() -> None:
    """The state PR A leaves behind, pinned so a later change cannot quietly restore
    the overstated verdicts. `create`/`update` keep their proofs — they always wrote
    a value and read it back; `read`/`filter`/`sort` do not, and `search` was
    withdrawn outright because its probe bypassed the facade's search contract."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    proven: dict[str, int] = {}
    for entity in (manifest.get("entities") or {}).values():
        for facets in (entity.get("fields") or {}).values():
            for facet, verdict in facets.items():
                if not facet.endswith("Note") and is_proven(verdict):
                    proven[facet] = proven.get(facet, 0) + 1
    assert proven == {"create": 199, "update": 203}, proven
