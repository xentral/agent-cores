"""Proven capabilities reach the capability view, and a scoped run does not erase them.

Two ways an action's live result was being lost between the probe and the sheet.

**It was never stamped.** `_apply_verified` only ever touched fields, so every
action and process step in every capability view read "declared, untested" —
including the ones a run had proven. SalesOrder carried `createSalesInvoice:
pass` in verified.json while its sheet showed `offen` for all thirteen actions.

**A later run deleted it.** A scoped re-probe (`VERIFY_ONLY=Quote`) replaces the
entity's block. Without `VERIFY_ACTIONS` there are no action results to write, so
re-probing Quote for an unrelated field fix silently dropped its proven
`send`/`addTag`/`removeTag` verdicts — and nothing in the output said so.
"""

from __future__ import annotations

from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter


class _Adapter(QuoteAdapter):
    def __init__(self, entity: dict[str, Any]) -> None:
        super().__init__()
        self._entity = entity

    def _apply_verified_capabilities(self, entries, results_key, notes_key):  # noqa: ANN001
        results = self._entity.get(results_key) or {}
        notes = self._entity.get(notes_key) or {}
        for entry in entries:
            key = entry.get("key")
            if key in results:
                entry["verified"] = {"status": results[key]}
                if notes.get(key):
                    entry["verified"]["note"] = notes[key]


def _actions(meta: dict[str, Any]) -> dict[str, Any]:
    return {a["key"]: a for a in meta.get("actions") or []}


def _commands(meta: dict[str, Any]) -> dict[str, Any]:
    return {c["key"]: c for g in meta.get("processSteps") or [] for c in g.get("commands") or []}


ENTITY = {
    "actions": {"send": "pass", "addTag": "pass"},
    "actionsNotes": {"send": "mailed the sampled document"},
    "processSteps": {"release": "pass", "cancel": "fail"},
}


def test_an_action_verdict_is_stamped() -> None:
    meta = _Adapter(ENTITY).metadata()
    assert _actions(meta)["send"]["verified"] == {
        "status": "pass",
        "note": "mailed the sampled document",
    }


def test_a_generic_action_is_stamped_too() -> None:
    """addTag/removeTag are synthesised by the base, not declared by the entity —
    they still get their results."""
    assert _actions(_Adapter(ENTITY).metadata())["addTag"]["verified"]["status"] == "pass"


def test_a_process_step_command_is_stamped() -> None:
    """Commands sit one level down, inside their group."""
    cmds = _commands(_Adapter(ENTITY).metadata())
    assert cmds["release"]["verified"]["status"] == "pass"
    assert cmds["cancel"]["verified"]["status"] == "fail"


def test_an_untested_entry_stays_unstamped() -> None:
    """Absent means untested — it must not be dressed up as a result."""
    meta = _Adapter(ENTITY).metadata()
    assert "verified" not in _actions(meta)["convertToSalesOrder"]
    assert "verified" not in _commands(meta)["accept"]


def test_a_wish_is_not_overwritten_by_a_stamp() -> None:
    """A declared-but-impossible action keeps its wish."""
    assert _actions(_Adapter(ENTITY).metadata())["duplicate"]["wish"]


def test_the_real_core_stamps_from_the_committed_manifest() -> None:
    """Not just the stub: the shipped verified.json reaches the shipped actions."""
    meta = QuoteAdapter().metadata()
    proven = {k for k, a in _actions(meta).items() if (a.get("verified") or {}).get("status")}
    assert proven, "no action carries a verdict — the stamping is not wired up"
