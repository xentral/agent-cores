"""A playbook may not claim a capability its core does not have.

``playbook.md`` is prose written for agents: it names entities, actions, statuses
and field paths so a workflow builder can go straight to the right call instead of
paying a `list` plus N speculative `describe`s. Prose of that kind is only useful
while it is true, and it is exactly the kind of file that rots quietly — the core
gains an action, an upstream gap closes, a status is renamed, and the document goes
on confidently instructing people to build the wrong thing.

So a playbook ends in a machine-checkable claims block (``## §9``), and this test is
what makes the prose trustworthy: every claim is replayed against the core's own
``metadata()``. It discovers playbooks rather than naming one, so a second core
gains the same guarantee by dropping the file in.

The rules that carry the most weight:

* **A claimed-executable action must carry no wish**, and a claimed wish must still
  be one. Getting this backwards in either direction is the expensive failure — a
  builder either designs around a capability that exists, or ships a workflow that
  is refused on the first real record.
* **The claims must be complete per entity.** For every entity the block mentions,
  the executable and wish lists together must cover *all* of its actions and step
  commands, so a newly built capability cannot go unmentioned. The wish-hygiene
  rule (``test_agentos_neo_xentral_wish_hygiene``) guards the same drift field-side.
* **A core whose roster is only knowable at request time may not carry claims at
  all.** Its adapters come from an ``adapters_factory`` that needs a live
  connection, so nothing here could check them and every claim would be an
  unverifiable assertion about someone else's instance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

CORES_DIR = Path(__file__).resolve().parent.parent / "cores"
PLAYBOOK = "playbook.md"

# Sections of the claims block and what each asserts. Anything else is a typo,
# not a new rule — see test_no_unknown_claim_sections.
KNOWN_SECTIONS = {
    "executable",
    "wishes",
    "operations",
    "statuses",
    "requiredForCreate",
    "filterable",
    "notFilterable",
    "fields",
    "commands",
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


def _claims(core_id: str) -> dict:
    text = (CORES_DIR / core_id / PLAYBOOK).read_text("utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, flags=re.S)
    if not blocks:
        return {}
    assert len(blocks) == 1, f"{core_id}: expected one ```yaml claims block, found {len(blocks)}"
    parsed = yaml.safe_load(blocks[0])
    assert isinstance(parsed, dict) and parsed, f"{core_id}: claims block must be a mapping"
    return parsed


def _model(core_id: str) -> dict:
    """Every adapter's metadata, indexed the way the claims block addresses it."""
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
    return {"id": core_id, "claims": _claims(core_id), "model": _model(core_id)}


def _entity(core: dict, key: str) -> dict:
    assert key in core["model"], (
        f"{core['id']}: the playbook names entity {key!r}, which this core does not expose"
    )
    return core["model"][key]


# `help` returns the generic entities guide (~9 KB) plus this file, in one response,
# on a call an agent makes early and once. A playbook that grows into a reference
# manual stops being read at all — so the budget is a design constraint, not a
# formality. Raise it deliberately, with a reason, never to make a commit pass.
#
# Moved once, from 48k, when the Neo playbook gained the back-office recipes a real
# clerk needs: find the customer, create an order that inherits from them, what is
# still changeable afterwards, partial orders, which number finds which document,
# traffic lights. That is the coverage the file exists for, not drift — and the
# alternative was cutting the filter contract or the status vocabularies, which are
# exactly what stop a workflow from silently matching nothing. Treat the recipes as
# the ceiling: next time, cut.
PLAYBOOK_BUDGET_BYTES = 56_000


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


def test_a_dynamic_roster_core_makes_no_claims(core):
    if _load_core(core["id"]).adapters_factory is None:
        return
    assert not core["claims"], (
        f"{core['id']}: this core resolves its roster live (adapters_factory), so nothing "
        f"here can check a claim about it — drop the claims block and keep the prose general"
    )


def test_no_unknown_claim_sections(core):
    unknown = set(core["claims"]) - KNOWN_SECTIONS
    assert not unknown, (
        f"{core['id']}: unknown claim sections {sorted(unknown)} — nothing checks these"
    )


def test_executable_capabilities_exist_and_carry_no_wish(core):
    for key, ops in (core["claims"].get("executable") or {}).items():
        entity = _entity(core, key)
        for op in ops:
            capability = entity["capabilities"].get(op)
            assert capability is not None, (
                f"{key}.{op}: the playbook calls this executable, but the core declares "
                f"no such action or step command"
            )
            assert not capability.get("wish"), (
                f"{key}.{op}: the playbook calls this executable, but the core marks it a "
                f"wish — {capability['wish']}"
            )


def test_wishes_are_still_wishes(core):
    for key, ops in (core["claims"].get("wishes") or {}).items():
        entity = _entity(core, key)
        for op in ops:
            capability = entity["capabilities"].get(op)
            assert capability is not None, (
                f"{key}.{op}: the playbook lists this as a wish, but the core declares no "
                f"such action or step command — drop it from the prose and the claims"
            )
            assert capability.get("wish"), (
                f"{key}.{op}: the playbook tells builders this is NOT possible, but the core "
                f"now implements it — move it to `executable` and fix the prose"
            )


def test_claims_cover_every_capability_of_the_entities_they_name(core):
    """A capability the playbook never mentions is a capability nobody will use."""
    executable = core["claims"].get("executable") or {}
    wishes = core["claims"].get("wishes") or {}
    for key in sorted(set(executable) | set(wishes)):
        entity = _entity(core, key)
        claimed = set(executable.get(key) or []) | set(wishes.get(key) or [])
        actual = set(entity["capabilities"])
        assert claimed == actual, (
            f"{key}: the playbook's claims and the core's capabilities disagree. "
            f"missing from the playbook: {sorted(actual - claimed) or 'none'}; "
            f"claimed but gone from the core: {sorted(claimed - actual) or 'none'}"
        )


def test_declared_operations_match(core):
    for key, ops in (core["claims"].get("operations") or {}).items():
        entity = _entity(core, key)
        assert sorted(entity["operations"]) == sorted(ops), (
            f"{key}: playbook says operations {sorted(ops)}, core exposes "
            f"{sorted(entity['operations'])}"
        )


def test_status_vocabularies_match(core):
    for dotted, values in (core["claims"].get("statuses") or {}).items():
        key, _, path = dotted.partition(".")
        spec = _entity(core, key)["fields"].get(path)
        assert spec is not None, f"{dotted}: no such field"
        options = spec.get("options") or []
        actual = {o.get("value") if isinstance(o, dict) else o for o in options}
        assert actual, f"{dotted}: the field declares no options"
        assert actual == set(values), (
            f"{dotted}: playbook lists {sorted(values)}, core offers {sorted(actual)}"
        )


def test_required_for_create_matches(core):
    for key, paths in (core["claims"].get("requiredForCreate") or {}).items():
        entity = _entity(core, key)
        actual = {
            path
            for path, spec in entity["fields"].items()
            if "required" in (spec.get("rules") or [])
        }
        assert actual == set(paths), (
            f"{key}: playbook says {sorted(paths)} are required to create, core requires "
            f"{sorted(actual)}"
        )


def test_filterable_claims_hold(core):
    for key, paths in (core["claims"].get("filterable") or {}).items():
        entity = _entity(core, key)
        for path in paths:
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            assert spec.get("filterable"), (
                f"{key}.{path}: the playbook offers this as a filter, but it is not filterable"
            )


def test_not_filterable_claims_hold(core):
    """The documented traps — a field that exists but cannot be queried."""
    for key, paths in (core["claims"].get("notFilterable") or {}).items():
        entity = _entity(core, key)
        for path in paths:
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            assert not spec.get("filterable"), (
                f"{key}.{path}: the playbook warns this cannot be filtered, but it now can — "
                f"the documented workaround is obsolete"
            )


def test_named_field_paths_resolve(core):
    for dotted in core["claims"].get("fields") or []:
        key, _, path = dotted.partition(".")
        assert path in _entity(core, key)["fields"], f"{dotted}: no such field path"


def test_command_shapes_match(core):
    for dotted, required in (core["claims"].get("commands") or {}).items():
        key, _, op = dotted.partition(".")
        capability = _entity(core, key)["capabilities"].get(op)
        assert capability is not None, f"{dotted}: no such action or step command"
        actual = (capability.get("command") or {}).get("required") or []
        assert sorted(actual) == sorted(required), (
            f"{dotted}: playbook says the command requires {sorted(required)}, core requires "
            f"{sorted(actual)}"
        )
