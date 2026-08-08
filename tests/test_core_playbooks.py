"""A core must do what its specification says — and prove it.

``capabilities.spec.yaml`` is the core's capability specification: what the ERP must
be able to do, written and signed off by a domain expert. This module replays every
statement in it against two independent sources — the core's own ``metadata()`` (what
the code declares) and ``verified.json`` (what a live run against a real tenant
actually demonstrated).

**The direction of proof runs from the spec to the code.** When the two disagree the
default reading is that the core is wrong, and the assertion messages say so. That is
a reversal: this file used to treat the core as ground truth and the document as a
claim about it, which meant every failure read as "go fix the prose".

The spec is deliberately NOT generated. It is checked against the very metadata a
generator would derive it from, so generating it would turn every assertion here into
a tautology that can never fail again — nine green tests guarding nothing. A spec
derived from the implementation also cannot state what is still missing, which is the
only reason a domain expert would read it.

``playbook.md`` is the prose companion, written for agents rather than people: it
names entities, actions and field paths so a workflow builder can go straight to the
right call. It is checked here too, because prose of that kind is only useful while
it is true. The machine-only statements live in the spec rather than in the prose so
the playbook stays short enough to be read in one pass.

The rules that carry the most weight:

* **A capability claimed executable must carry no wish**, and a claimed wish must
  still be one. Getting this backwards in either direction is the expensive failure —
  a builder either designs around a capability that exists, or ships a workflow that
  is refused on the first real record.
* **"Executable" is a claim about the code, not about reality.** ``evidenceGaps``
  records which of those capabilities no live run has actually proven, and must match
  ``verified.json`` exactly in both directions. Without it, a route that merely exists
  reads identically to one whose effect was observed — measured on the committed file
  when this rule was written: of 80 capabilities claimed executable, 25 were proven.
* **The spec must be complete over the core**, not merely over the entities it
  happens to mention: every entity with capabilities, every CRUD surface, every
  parameter-carrying command. Partial coverage is the dangerous kind, because a
  reviewer reads a subset and believes they reviewed the whole.
* **A core whose roster is only knowable at request time may not carry a spec at
  all.** Its adapters come from an ``adapters_factory`` that needs a live
  connection, so nothing here could check them and every statement would be an
  unverifiable assertion about someone else's instance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

CORES_DIR = Path(__file__).resolve().parent.parent / "cores"
PLAYBOOK = "playbook.md"
SPEC = "capabilities.spec.yaml"
VERIFIED = "verified.json"

#: The verdict that means the capability itself was demonstrated — an effect was
#: read back, not merely a route that answered. Mirrors ``verdicts.PROVEN``; kept
#: as a literal so this module stays importable without the core package.
PROVEN = "pass"

# Sections of the spec and what each asserts. Anything else is a typo, not a new
# rule — see test_no_unknown_spec_sections.
KNOWN_SECTIONS = {
    "executable",
    "wishes",
    "evidenceGaps",
    "operations",
    "statuses",
    "requiredForCreate",
    "filterable",
    "notFilterable",
    "fields",
    "commands",
    "reviewed",
}

CORES_WITH_PLAYBOOK = sorted(p.parent.name for p in CORES_DIR.glob(f"*/{PLAYBOOK}"))


def _load_core(core_id: str):
    module = __import__(f"xentral_entity_cores.{core_id}.manifest", fromlist=["CORE"])
    return module.CORE


def _walk(props, prefix=""):
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        yield path, spec
        node = spec.get("node")
        nested = node.get("properties") if isinstance(node, dict) else spec.get("properties")
        if isinstance(nested, dict):
            yield from _walk(nested, f"{path}.")


def _spec(core_id: str) -> dict:
    """The specification lives in a sibling `capabilities.spec.yaml`, not in the prose.

    It used to be a fenced block at the end of the playbook, which put ~9 KB of
    machine-only content into a document whose whole point is being short enough to
    be read in one pass. A core may ship prose without a spec (nothing to check); a
    spec without a playbook is a packaging accident and fails loudly.
    """
    path = CORES_DIR / core_id / SPEC
    if not path.is_file():
        return {}
    parsed = yaml.safe_load(path.read_text("utf-8"))
    assert isinstance(parsed, dict) and parsed, f"{core_id}: {SPEC} must be a mapping"
    return parsed


def _verified(core_id: str) -> dict:
    """Per-entity live-probe verdicts, actions and step commands merged into one map.

    The manifest keeps them apart (``actions`` / ``processSteps``) because they come
    from different places in the schema; a capability key is unique across both (the
    model builder asserts it), so the spec addresses them as one namespace.
    """
    path = CORES_DIR / core_id / VERIFIED
    if not path.is_file():
        return {}
    entities = json.loads(path.read_text("utf-8")).get("entities") or {}
    return {
        key: {**(entry.get("actions") or {}), **(entry.get("processSteps") or {})}
        for key, entry in entities.items()
    }


def _model(core_id: str) -> dict:
    """Every adapter's metadata, indexed the way the spec addresses it."""
    out: dict[str, dict] = {}
    for adapter in _load_core(core_id).adapters:
        meta = adapter.metadata(None)
        capabilities: dict[str, dict] = {}
        for action in meta.get("actions") or []:
            if action.get("key"):
                capabilities[action["key"]] = action
        for group in meta.get("processSteps") or []:
            for command in group.get("commands") or []:
                key = command.get("key")
                if not key:
                    continue
                assert key not in capabilities, (
                    f"{meta['key']}: {key} is both an action and a step command"
                )
                capabilities[key] = command
        out[meta["key"]] = {
            "operations": list(meta.get("operations") or []),
            "capabilities": capabilities,
            "fields": dict(_walk(meta.get("rootNode", {}).get("properties", {}))),
        }
    return out


@pytest.fixture(scope="module", params=CORES_WITH_PLAYBOOK)
def core(request):
    core_id = request.param
    return {
        "id": core_id,
        "spec": _spec(core_id),
        "model": _model(core_id),
        "verified": _verified(core_id),
    }


def _entity(core: dict, key: str) -> dict:
    assert key in core["model"], (
        f"{core['id']}: the spec requires entity {key!r}, which this core does not expose"
    )
    return core["model"][key]


# `help` returns the generic entities guide (~9 KB) plus this file, in one response,
# on a call an agent makes early and once. A playbook that grows into a reference
# manual stops being read at all — so the budget is a design constraint, not a
# formality.
#
# It went 48k → 56k once while the recipes were being written, then back to 18k when
# the machine-only statements moved out to capabilities.spec.yaml and the prose was
# rewritten to the 80 % an e-commerce back office actually does. Roughly three A4
# pages. That is the target shape: everything past it belongs in `describe`, which
# is live and always right, or in the spec, which is read by reviewers, not agents.
PLAYBOOK_BUDGET_BYTES = 18_000


def test_playbook_stays_within_its_budget(core):
    size = (CORES_DIR / core["id"] / PLAYBOOK).stat().st_size
    assert size <= PLAYBOOK_BUDGET_BYTES, (
        f"{core['id']}: playbook is {size} bytes, over the {PLAYBOOK_BUDGET_BYTES} budget. "
        f"Cut a section or move detail into `describe` — do not just raise the number."
    )


def test_at_least_one_core_ships_a_playbook():
    """Guards the discovery itself: a rename of the file would otherwise turn this
    whole module into a silent no-op that still reports green."""
    assert CORES_WITH_PLAYBOOK, f"no core ships a {PLAYBOOK}"


def test_a_dynamic_roster_core_carries_no_spec(core):
    if _load_core(core["id"]).adapters_factory is None:
        return
    assert not core["spec"], (
        f"{core['id']}: this core resolves its roster live (adapters_factory), so nothing "
        f"here can check a statement about it — drop {SPEC} and keep the prose general"
    )


def test_no_unknown_spec_sections(core):
    unknown = set(core["spec"]) - KNOWN_SECTIONS
    assert not unknown, (
        f"{core['id']}: unknown spec sections {sorted(unknown)} — nothing checks these"
    )


def test_executable_capabilities_exist_and_carry_no_wish(core):
    for key, ops in (core["spec"].get("executable") or {}).items():
        entity = _entity(core, key)
        for op in ops:
            capability = entity["capabilities"].get(op)
            assert capability is not None, (
                f"{key}.{op}: the spec requires this capability, but the core declares no "
                f"such action or step command — build it, or move it to `wishes` with the "
                f"upstream reason"
            )
            assert not capability.get("wish"), (
                f"{key}.{op}: the spec requires this to work, but the core marks it a "
                f"wish — {capability['wish']}"
            )


def test_wishes_are_still_wishes(core):
    for key, ops in (core["spec"].get("wishes") or {}).items():
        entity = _entity(core, key)
        for op in ops:
            capability = entity["capabilities"].get(op)
            assert capability is not None, (
                f"{key}.{op}: the spec records this as an upstream gap, but the core declares "
                f"no such action or step command — a gap has to be declared to be visible in "
                f"`describe`"
            )
            assert capability.get("wish"), (
                f"{key}.{op}: the spec tells builders this is NOT possible, but the core now "
                f"implements it — move it to `executable` and fix the prose"
            )


def test_evidence_gaps_match_the_live_run(core):
    """Which executable capabilities a live run has NOT actually proven.

    ``executable`` is a statement about the code: the adapter declares the capability
    and does not mark it a wish. That is not the same as it working. The probe records
    how strongly each one was shown (``verdicts.py``) — and ``reachable``, the most
    common verdict, explicitly means the route exists and refused our probe, with no
    capability claim in either direction.

    So the gap list is spec'd and checked in both directions. A capability that gains
    a real proof must be struck from it; a newly claimed one that has never been proven
    must be added. Either way a human writes the line, which is the point: the number of
    unproven capabilities cannot drift upward quietly.
    """
    if not core["verified"]:
        return
    declared = {
        f"{key}.{op}" for key, ops in (core["spec"].get("evidenceGaps") or {}).items() for op in ops
    }
    actual = {
        f"{key}.{op}"
        for key, ops in (core["spec"].get("executable") or {}).items()
        for op in ops
        if (core["verified"].get(key) or {}).get(op) != PROVEN
    }
    assert declared == actual, (
        f"{core['id']}: `evidenceGaps` disagrees with {VERIFIED}. "
        f"now proven, strike from the spec: {sorted(declared - actual) or 'none'}; "
        f"claimed executable but never proven, add them: {sorted(actual - declared) or 'none'}"
    )


def test_every_entity_with_capabilities_is_specified(core):
    """Completeness over the CORE, not over the entities the spec happens to name.

    Partial coverage is the dangerous kind: a reviewer reads what is there and takes it
    for the whole surface. An entity that grows its first action must appear here before
    anyone can use it.
    """
    specified = set(core["spec"].get("executable") or {}) | set(core["spec"].get("wishes") or {})
    unspecified = sorted(
        key
        for key, entity in core["model"].items()
        if entity["capabilities"] and key not in specified
    )
    assert not unspecified, (
        f"{core['id']}: these entities have actions or step commands that no spec section "
        f"mentions: {unspecified}"
    )


def test_spec_covers_every_capability_of_the_entities_it_names(core):
    """A capability the spec never mentions is a capability nobody reviewed."""
    executable = core["spec"].get("executable") or {}
    wishes = core["spec"].get("wishes") or {}
    for key in sorted(set(executable) | set(wishes)):
        entity = _entity(core, key)
        specified = set(executable.get(key) or []) | set(wishes.get(key) or [])
        actual = set(entity["capabilities"])
        assert specified == actual, (
            f"{key}: the spec and the core's capabilities disagree. "
            f"built but unspecified: {sorted(actual - specified) or 'none'}; "
            f"specified but gone from the core: {sorted(specified - actual) or 'none'}"
        )


def test_declared_operations_match(core):
    for key, ops in (core["spec"].get("operations") or {}).items():
        entity = _entity(core, key)
        assert sorted(entity["operations"]) == sorted(ops), (
            f"{key}: the spec requires operations {sorted(ops)}, core exposes "
            f"{sorted(entity['operations'])}"
        )


def test_operations_are_specified_for_every_entity(core):
    """Read-only versus writable is a business decision, so every entity states it.

    Left as a sample this section reads as "these are the interesting ones", when what
    it actually means is "nobody wrote down the rest".
    """
    if not core["spec"]:
        return
    missing = sorted(set(core["model"]) - set(core["spec"].get("operations") or {}))
    assert not missing, (
        f"{core['id']}: no operations specified for {missing} — state the CRUD surface "
        f"for every entity the core exposes"
    )


def test_status_vocabularies_match(core):
    for dotted, values in (core["spec"].get("statuses") or {}).items():
        key, _, path = dotted.partition(".")
        spec = _entity(core, key)["fields"].get(path)
        assert spec is not None, f"{dotted}: no such field"
        options = spec.get("options") or []
        actual = {o.get("value") if isinstance(o, dict) else o for o in options}
        assert actual, f"{dotted}: the field declares no options"
        assert actual == set(values), (
            f"{dotted}: the spec requires {sorted(values)}, core offers {sorted(actual)}"
        )


def test_required_for_create_matches(core):
    for key, paths in (core["spec"].get("requiredForCreate") or {}).items():
        entity = _entity(core, key)
        actual = {
            path
            for path, spec in entity["fields"].items()
            if "required" in (spec.get("rules") or [])
        }
        assert actual == set(paths), (
            f"{key}: the spec requires {sorted(paths)} on create, core requires {sorted(actual)}"
        )


def test_filterable_requirements_hold(core):
    for key, paths in (core["spec"].get("filterable") or {}).items():
        entity = _entity(core, key)
        for path in paths:
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            assert spec.get("filterable"), (
                f"{key}.{path}: the spec requires this as a filter, but it is not filterable"
            )


def test_not_filterable_records_hold(core):
    """The documented traps — a field that exists but cannot be queried."""
    for key, paths in (core["spec"].get("notFilterable") or {}).items():
        entity = _entity(core, key)
        for path in paths:
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            assert not spec.get("filterable"), (
                f"{key}.{path}: the spec records this as unfilterable, but it now can be — "
                f"the documented workaround is obsolete"
            )


def test_named_field_paths_resolve(core):
    for dotted in core["spec"].get("fields") or []:
        key, _, path = dotted.partition(".")
        assert path in _entity(core, key)["fields"], f"{dotted}: no such field path"


def test_command_shapes_match(core):
    for dotted, required in (core["spec"].get("commands") or {}).items():
        key, _, op = dotted.partition(".")
        capability = _entity(core, key)["capabilities"].get(op)
        assert capability is not None, f"{dotted}: no such action or step command"
        actual = (capability.get("command") or {}).get("required") or []
        assert sorted(actual) == sorted(required), (
            f"{dotted}: the spec requires parameters {sorted(required)}, core requires "
            f"{sorted(actual)}"
        )


def test_commands_cover_every_parameterised_capability(core):
    """Every capability that takes parameters states them here.

    This section used to be a sample, and the asymmetry misled a reader: `Quote.addTag`
    was listed while `Quote.removeTag` — the same generated schema, the same required
    `title` — was not, which reads as a difference between the two capabilities. There
    is none. Listing all of them is what makes their absence meaningful.
    """
    if not core["spec"]:
        return
    specified = set(core["spec"].get("commands") or {})
    missing = sorted(
        f"{key}.{op}"
        for key, entity in core["model"].items()
        for op, capability in entity["capabilities"].items()
        if (capability.get("command") or {}).get("required") and f"{key}.{op}" not in specified
    )
    assert not missing, (
        f"{core['id']}: these capabilities take required parameters that the spec does not "
        f"state: {missing}"
    )


def test_reviewed_ledger_names_real_entities(core):
    """The sign-off ledger: which entities a domain expert actually went through.

    Deliberately not a gate — it does not fail a build for being empty, because an
    unreviewed core is a normal state and pretending otherwise would just get the
    ledger filled to make CI quiet. What it must not do is name something that does
    not exist, which would let a sign-off point at nothing.
    """
    for key in core["spec"].get("reviewed") or {}:
        _entity(core, key)
