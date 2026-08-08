"""A core must do what its specification says — and prove it.

``erp-spec.yaml`` is the core's ERP specification: what the system must be able to do
and to record, written and signed off by a domain expert. This module replays every
statement in it against two independent sources — the core's own ``metadata()`` (what
the code declares) and ``verified.json`` (what a live run against a real tenant
actually demonstrated).

It is organised ``category → entity → everything about that entity``, so one block
answers "what can this system do with an order". Every rule below addresses a single
entity, so ``_spec`` flattens the grouping once and exactly one rule checks it — the
categories are the adapters' own ``manifest.category``, not a taxonomy the file owns.

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

The document itself is read, not just a list describing it: every dotted field path
and every ``Entity.name`` reference in its code spans must resolve against the core.
That is the difference between checking a hand-written mirror of the prose and
checking the prose. It does NOT extend to bare backticked words — most of them are
tool names, filter operators and status values rather than fields, and a rule over
them would need a ~67-entry exclusion list, which is the hand-maintenance being
removed. Nor does it bind a path to an entity: the prose writes
`references.customerOrderNumber` under a heading about orders, never
`SalesOrder.references.customerOrderNumber`, and inferring the entity from document
structure would assert a claim the text does not make.

The rules that carry the most weight:

* **A capability claimed executable must carry no wish**, and a claimed wish must
  still be one. Getting this backwards in either direction is the expensive failure —
  a builder either designs around a capability that exists, or ships a workflow that
  is refused on the first real record.
* **A capability in ``can`` is a claim about the code, not about reality.** Each one
  records the verdict a live run returned, checked against ``verified.json`` by value.
  Without that, a route that merely exists reads identically to one whose effect was
  observed — measured when this was written: of 80 capabilities, 25 proven, 34 only
  `reachable`, 3 `executed`, 18 never tested at all.
* **The spec must be complete over the core**, not merely over the entities it
  happens to mention: one block per entity, every CRUD surface, every capability,
  every parameter-carrying command. Partial coverage is the dangerous kind, because a
  reviewer reads a subset and believes they reviewed the whole.
* **A core whose roster is only knowable at request time may not carry a spec at
  all.** Its adapters come from an ``adapters_factory`` that needs a live
  connection, so nothing here could check them and every statement would be an
  unverifiable assertion about someone else's instance.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import pytest
import yaml

CORES_DIR = Path(__file__).resolve().parent.parent / "cores"
PLAYBOOK = "playbook.md"
SPEC = "erp-spec.yaml"
VERIFIED = "verified.json"

#: The verdict that means the capability itself was demonstrated — an effect was
#: read back, not merely a route that answered. Mirrors ``verdicts.PROVEN``; kept
#: as a literal so this module stays importable without the core package.
PROVEN = "pass"

#: The grouping the spec may use. Not a taxonomy the file owns — these are the
#: adapters' own ``manifest.category`` values, the same ones `describe` ships.
CATEGORIES = ("documents", "masterdata", "crm", "settings")

#: What an entity block may state. Anything else is a typo, not a new rule — see
#: test_no_unknown_categories_or_entity_keys.
ENTITY_KEYS = frozenset(
    {
        "label",
        "reviewed",
        "operations",
        "statuses",
        "requiredForCreate",
        "filterable",
        "notFilterable",
        "fields",
        "can",
        "cannot",
        "fieldGaps",
    }
)


class StrictLoader(yaml.SafeLoader):
    """``safe_load`` lets a duplicate mapping key win silently.

    Harmless while an entity appeared once per section across eleven small sections.
    In one 1 700-line file with 50 sibling blocks, a copy-pasted ``Customer:`` would
    delete an entity's entire specification and still parse clean.
    """


def _no_duplicate_keys(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.YAMLError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)

CORES_WITH_PLAYBOOK = sorted(p.parent.name for p in CORES_DIR.glob(f"*/{PLAYBOOK}"))

#: Inline code spans, and the two token shapes inside them that make a checkable
#: claim about the core: a dotted field path, and an `Entity.name` reference.
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_DOTTED_PATH = re.compile(r"^[a-z][A-Za-z0-9_]*(\[\])?(\.[A-Za-z0-9_]+(\[\])?)+$")
_QUALIFIED = re.compile(r"\b([A-Z][A-Za-z]+)\.([A-Za-z][A-Za-z0-9_.]*)")

#: Dotted tokens whose head is one of these describes the SHAPE of a `describe`
#: response — `actions[].key`, `query.searchable` — not a field on a record. Excluded
#: by their head rather than as literal strings, so a new example phrased differently
#: does not need a new entry.
_DESCRIBE_VOCABULARY = frozenset({"actions", "processSteps", "query", "rootNode", "command"})

#: A dotted token ending in one of these is a filename the prose points at.
_DOC_SUFFIXES = (".yaml", ".yml", ".json", ".md", ".py", ".csv", ".xlsx")


def _playbook(core_id: str) -> str:
    return (CORES_DIR / core_id / PLAYBOOK).read_text("utf-8")


def _load_core(core_id: str):
    module = __import__(f"xentral_entity_cores.{core_id}.manifest", fromlist=["CORE"])
    return module.CORE


def _chain_order(core_id: str):
    """The core's own reading sequence (``order.chain_order``), or None.

    Imported from the core rather than restated here: the exporter that writes
    `review.xlsx` uses the same function, and a test guarding its own second copy of a
    sequence guards nothing. A core that ships no `order` module simply states no order,
    and the rule skips.
    """
    try:
        module = __import__(f"xentral_entity_cores.{core_id}.order", fromlist=["chain_order"])
    except ImportError:
        return None
    return module.chain_order


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


def _categories(core_id: str) -> dict:
    """The raw ``category → {entity: block}`` document, order preserved.

    Kept alongside the flattened view because two rules are about the grouping itself:
    which categories exist, and in what order the entities inside one are written.
    """
    path = CORES_DIR / core_id / SPEC
    if not path.is_file():
        return {}
    parsed = yaml.load(path.read_text("utf-8"), StrictLoader)  # noqa: S506
    assert isinstance(parsed, dict) and parsed, f"{core_id}: {SPEC} must be a mapping"
    return parsed


def _spec(core_id: str) -> dict:
    """Entity key → its specification block, flattened across the category grouping.

    The file groups by category because that is how a reviewer navigates 50 entities;
    every rule below addresses ONE entity. Flattening here means the grouping is
    checked by exactly one rule
    (``test_every_entity_sits_under_the_category_its_manifest_declares``) instead of
    being re-derived by all of them.

    A core may ship prose without a spec (nothing to check); a spec without a playbook
    is a packaging accident and fails loudly.
    """
    out: dict[str, dict] = {}
    for category, entities in _categories(core_id).items():
        assert isinstance(entities, dict), f"{core_id}: category {category!r} is not a mapping"
        for key, block in entities.items():
            assert isinstance(block, dict), f"{core_id}: {category}.{key} is not a mapping"
            out[key] = {**block, "_category": category}
    return out


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
            # `category` and `label` are the core's own; the spec mirrors them and is
            # checked against them, so neither becomes a second source of truth.
            "category": meta.get("category"),
            "label": meta.get("label"),
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
        "categories": _categories(core_id),
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
# the machine-only statements moved out to erp-spec.yaml and the prose was
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


def test_no_unknown_categories_or_entity_keys(core):
    unknown_categories = sorted(set(core["categories"]) - set(CATEGORIES))
    assert not unknown_categories, (
        f"{core['id']}: unknown categories {unknown_categories} — the grouping is the "
        f"core's own `manifest.category`, not a taxonomy this file may invent"
    )
    unknown = sorted(
        f"{key}.{k}"
        for key, block in core["spec"].items()
        for k in block
        if not k.startswith("_") and k not in ENTITY_KEYS
    )
    assert not unknown, f"{core['id']}: unknown keys {unknown} — nothing checks these"


def test_executable_capabilities_exist_and_carry_no_wish(core):
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for op in block.get("can") or {}:
            capability = entity["capabilities"].get(op)
            assert capability is not None, (
                f"{key}.{op}: the spec requires this capability, but the core declares no "
                f"such action or step command — build it, or move it to `cannot` with the "
                f"upstream reason"
            )
            assert not capability.get("wish"), (
                f"{key}.{op}: the spec requires this to work, but the core marks it a "
                f"wish — {capability['wish']}"
            )


def test_wishes_are_still_wishes(core):
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for op in block.get("cannot") or {}:
            capability = entity["capabilities"].get(op)
            assert capability is not None, (
                f"{key}.{op}: the spec records this as an upstream gap, but the core declares "
                f"no such action or step command — a gap has to be declared to be visible in "
                f"`describe`"
            )
            assert capability.get("wish"), (
                f"{key}.{op}: the spec tells builders this is NOT possible, but the core now "
                f"implements it — move it to `can` and fix the prose"
            )


def test_every_wish_carries_a_reason(core):
    """A recorded gap must say WHY, in the spec, where its owner can edit it.

    The reason used to live in the adapter as a prose block on ``wish=``. That put a
    business statement — "the transition happens only in the UI", "digest-authenticated
    and behind a killswitch" — in a Python file the person who owns the requirement
    cannot touch. It now sits beside the key in ``wishes``, and the adapter only
    classifies.

    Without this rule the move would fail open: an entry with no text renders a
    placeholder naming the omission, which is honest at runtime but must not survive a
    build. A gap with no reason cannot be told apart from one nobody investigated.
    """
    for key, block in core["spec"].items():
        ops = block.get("cannot") or {}
        assert isinstance(ops, dict), (
            f"{key}: `cannot` must map each capability to its reason, not list bare keys"
        )
        for op, reason in ops.items():
            assert isinstance(reason, str) and reason.strip(), (
                f"{key}.{op}: recorded as an upstream gap with no reason. Say what the "
                f"upstream cannot do and how that was established."
            )
            assert "records no reason" not in reason, (
                f"{key}.{op}: carries the runtime placeholder as its reason"
            )


def test_no_rendered_wish_carries_the_runtime_placeholder(core):
    """The reason must survive the LOADER, not merely exist in the file.

    The rule above reads the spec directly. It would stay green through a rename, a
    re-nesting, or a category typo that orphans an entity — while `_wish_reasons()`
    returns nothing and every gap in `describe` renders "Declared as not executable,
    but ... records no reason". The text is right there in the file the whole time.

    The loader fails soft on purpose: it runs lazily on the request path, so a spec
    typo must cost a sentence, never a 500 from `describe` (``base.py`` records that
    trade where the fallback is written). This is the other half of that bargain —
    soft in production, loud here. It asserts on the rendered ``metadata()``, which is
    the exact code path `action_def` and `step_cmd` take in production, so no new
    machinery is needed to cover it.
    """
    for key, entity in core["model"].items():
        for op, capability in entity["capabilities"].items():
            wish = capability.get("wish")
            if not wish:
                continue
            assert "records no reason" not in wish, (
                f"{key}.{op}: the core declares this a gap, but the runtime loader could "
                f"not find its reason in {SPEC}. The file may be perfectly fine and the "
                f"loader wrong — check the shape, not just the text."
            )


def test_evidence_matches_the_live_run(core):
    """The recorded evidence must be the verdict the probe actually returned.

    `evidenceGaps` used to be a boundary — in the list or not — which meant `reachable`
    (the route exists and refused our probe: no capability claim either way), `executed`
    (something changed, nobody looked) and *never tested at all* were one word. Measured
    when this was rewritten: 34 reachable, 3 executed, 18 untested, all reading
    identically. The verdict vocabulary exists to tell those apart (`verdicts.py`), so
    the spec now records the verdict itself.

    Checked by value and in both directions: a capability that gains a proof must be
    upgraded here, one that loses it must be downgraded. A human writes the line either
    way, which is the point — the number of unproven capabilities cannot drift upward
    quietly.
    """
    if not core["verified"]:
        return
    drifted = {}
    for key, block in core["spec"].items():
        for op, entry in (block.get("can") or {}).items():
            recorded = entry.get("evidence") if isinstance(entry, dict) else None
            measured = (core["verified"].get(key) or {}).get(op)
            if recorded != measured:
                drifted[f"{key}.{op}"] = (recorded, measured)
    assert not drifted, (
        f"{core['id']}: `evidence` disagrees with {VERIFIED} (spec, live): "
        f"{dict(sorted(drifted.items()))}"
    )


def test_the_spec_covers_every_entity_and_invents_none(core):
    """One block per entity the core exposes, and no block for anything else.

    Partial coverage is the dangerous kind: a reviewer reads what is there and takes it
    for the whole surface. Entity-major makes this the natural shape of the rule — an
    entity that grows its first action already has a block waiting for it.
    """
    if not core["spec"]:
        return
    missing = sorted(set(core["model"]) - set(core["spec"]))
    invented = sorted(set(core["spec"]) - set(core["model"]))
    assert not missing and not invented, (
        f"{core['id']}: no block for {missing or 'none'}; blocks for entities this core "
        f"does not expose: {invented or 'none'}"
    )


def test_spec_covers_every_capability_of_every_entity(core):
    """A capability the spec never mentions is a capability nobody reviewed."""
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        specified = set(block.get("can") or {}) | set(block.get("cannot") or {})
        actual = set(entity["capabilities"])
        assert specified == actual, (
            f"{key}: the spec and the core's capabilities disagree. "
            f"built but unspecified: {sorted(actual - specified) or 'none'}; "
            f"specified but gone from the core: {sorted(specified - actual) or 'none'}"
        )


def test_declared_operations_match(core):
    """Stated for every entity: read-only versus writable is a business decision, and
    left as a sample the section reads as "these are the interesting ones" when it
    means "nobody wrote down the rest"."""
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        assert "operations" in block, (
            f"{key}: no `operations` — say whether this entity is read-only or writable"
        )
        assert sorted(entity["operations"]) == sorted(block["operations"]), (
            f"{key}: the spec requires operations {sorted(block['operations'])}, core "
            f"exposes {sorted(entity['operations'])}"
        )


def test_status_vocabularies_match(core):
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for path, values in (block.get("statuses") or {}).items():
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            options = spec.get("options") or []
            actual = {o.get("value") if isinstance(o, dict) else o for o in options}
            assert actual, f"{key}.{path}: the field declares no options"
            assert actual == set(values), (
                f"{key}.{path}: the spec requires {sorted(values)}, core offers {sorted(actual)}"
            )


def test_required_for_create_matches(core):
    for key, block in core["spec"].items():
        if "requiredForCreate" not in block:
            continue
        entity = _entity(core, key)
        actual = {
            path
            for path, spec in entity["fields"].items()
            if "required" in (spec.get("rules") or [])
        }
        assert actual == set(block["requiredForCreate"]), (
            f"{key}: the spec requires {sorted(block['requiredForCreate'])} on create, "
            f"core requires {sorted(actual)}"
        )


def test_required_for_create_is_stated_wherever_the_core_requires_anything(core):
    """Checked in both directions per entity that states it — which means dropping the
    whole block is invisible. This closes that one-way hole."""
    if not core["spec"]:
        return
    missing = sorted(
        key
        for key, entity in core["model"].items()
        if any("required" in (s.get("rules") or []) for s in entity["fields"].values())
        and "requiredForCreate" not in (core["spec"].get(key) or {})
    )
    assert not missing, (
        f"{core['id']}: these entities have mandatory create fields nobody wrote down: {missing}"
    )


def test_filterable_requirements_hold(core):
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for path in block.get("filterable") or []:
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            assert spec.get("filterable"), (
                f"{key}.{path}: the spec requires this as a filter, but it is not filterable"
            )


def test_not_filterable_records_hold(core):
    """The documented traps — a field that exists but cannot be queried."""
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for path in block.get("notFilterable") or []:
            spec = entity["fields"].get(path)
            assert spec is not None, f"{key}.{path}: no such field"
            assert not spec.get("filterable"), (
                f"{key}.{path}: the spec records this as unfilterable, but it now can be — "
                f"the documented workaround is obsolete"
            )


def test_named_field_paths_resolve(core):
    """`fields` is the paths the REVIEWER decided must exist — deliberately a sample,
    not an inventory of the model. The entity binding is structural now; it used to be
    a string prefix parsed with `partition`."""
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for path in block.get("fields") or []:
            assert path in entity["fields"], f"{key}.{path}: no such field path"


def test_playbook_field_paths_exist(core):
    """Every dotted path the PROSE names in backticks must be a real field.

    ``fields`` above checks a hand-written list of entity-qualified paths — the
    reviewer's statement about the model. This checks the document itself, which is
    the thing that actually rots: a field renamed upstream leaves the playbook
    instructing builders to bind something that is no longer there.

    Only dotted tokens are judged, and that limit is deliberate. Bare backticked
    words are mostly not fields at all — tool names, filter operators, status values,
    schema flags, step-group keys — and measured on this playbook a rule over them
    would need a ~67-entry exclusion list, which is the hand-maintenance this replaces.
    Dotted tokens are unambiguous: 25 of them, 21 real field paths, and the four
    exceptions are each a different KIND of thing, excluded by rule rather than by
    name (see the two constants).

    Entity binding is not checked here, because the prose does not carry it: it writes
    `references.customerOrderNumber` under a heading about orders, not
    `SalesOrder.references.customerOrderNumber`. Inferring the entity from document
    structure would invent a claim the text does not make. So this asserts the weaker,
    true thing — the path exists somewhere in the core — and the hand-written list
    keeps the per-entity binding.
    """
    known = {path for entity in core["model"].values() for path in entity["fields"]}
    if not known:
        return
    unresolved = set()
    for token in _BACKTICKED.findall(_playbook(core["id"])):
        if not _DOTTED_PATH.match(token) or token.endswith(_DOC_SUFFIXES):
            continue
        # `actions[].key` — strip the collection marker BEFORE reading the head, or
        # the vocabulary check compares against "actions[]" and never matches.
        path = token.replace("[]", "")
        if path.split(".", 1)[0] in _DESCRIBE_VOCABULARY or path in known:
            continue
        unresolved.add(token)
    unresolved = sorted(unresolved)
    assert not unresolved, (
        f"{core['id']}: the playbook names field paths this core does not have: "
        f"{unresolved}. Either the prose is stale, or the path moved."
    )


def test_playbook_qualified_references_resolve(core):
    """`Entity.something` in the prose must be one of that entity's own names.

    The few places the playbook does qualify a reference are the ones a builder copies
    straight into a call, so a stale one costs a failed run. Nothing checked them until
    now. A qualified token may name a field, a capability or an operation — the prose
    uses all three shapes — and naming none of them means the document is describing a
    core that does not exist.
    """
    unresolved = []
    for entity_key, tail in _QUALIFIED.findall(_playbook(core["id"])):
        entity = core["model"].get(entity_key)
        if entity is None:  # a capitalised word that is not an entity of this core
            continue
        tail = tail.rstrip(".")
        if (
            tail in entity["fields"]
            or tail in entity["capabilities"]
            or tail in entity["operations"]
        ):
            continue
        unresolved.append(f"{entity_key}.{tail}")
    assert not unresolved, (
        f"{core['id']}: the playbook names {sorted(set(unresolved))}, which are neither "
        f"fields nor capabilities nor operations of those entities"
    )


def test_command_shapes_match(core):
    """`params` names what a capability requires. The "no such capability" assertion the
    old dotted-key form needed is gone: a `can` key is already proven to be a real
    capability by `test_spec_covers_every_capability_of_every_entity`."""
    for key, block in core["spec"].items():
        entity = _entity(core, key)
        for op, entry in (block.get("can") or {}).items():
            required = (entry or {}).get("params") or []
            actual = (entity["capabilities"][op].get("command") or {}).get("required") or []
            assert sorted(actual) == sorted(required), (
                f"{key}.{op}: the spec requires parameters {sorted(required)}, core "
                f"requires {sorted(actual)}"
            )


def test_commands_cover_every_parameterised_capability(core):
    """Every capability that takes parameters states them.

    This used to be a separate section and a sample, and the asymmetry misled a reader:
    `Quote.addTag` was listed while `Quote.removeTag` — the same generated schema, the
    same required `title` — was not, which reads as a difference between the two
    capabilities. There is none. Stating all of them is what makes an absence mean
    something.
    """
    if not core["spec"]:
        return
    missing = sorted(
        f"{key}.{op}"
        for key, entity in core["model"].items()
        for op, capability in entity["capabilities"].items()
        if (capability.get("command") or {}).get("required")
        and not ((core["spec"].get(key) or {}).get("can") or {}).get(op, {}).get("params")
    )
    assert not missing, (
        f"{core['id']}: these capabilities take required parameters that the spec does not "
        f"state: {missing}"
    )


def test_the_reviewed_ledger_records_a_date_or_nothing(core):
    """The sign-off: when a domain expert went through this entity.

    Deliberately not a gate — it does not fail a build for being empty, because an
    unreviewed core is a normal state and pretending otherwise would just get the ledger
    filled to make CI quiet. What it must not hold is `true`: a sign-off that cannot say
    WHEN it happened is worth nothing a year later, when the entity has moved on.

    It sits on the entity now rather than in a register at the end of the file, so the
    reviewer records the fact where they established it.
    """
    for key, block in core["spec"].items():
        assert "reviewed" in block, (
            f"{key}: no `reviewed` key. Absent and 'never reviewed' must not look the "
            f"same — write `reviewed: null`."
        )
        reviewed = block["reviewed"]
        if reviewed is None:
            continue
        assert isinstance(reviewed, (str, datetime.date)) and str(reviewed).strip(), (
            f"{key}: `reviewed` must be the date of the sign-off (or null), not {reviewed!r}"
        )


def test_every_entity_sits_under_the_category_its_manifest_declares(core):
    """The grouping is not a taxonomy this file owns.

    It is `manifest.category` — the same value `describe` ships. Two owners for one fact
    is how a reviewer reads `documents`, believes they have seen every document, and
    misses one that someone filed next to `Batch` because it felt related.
    """
    misfiled = {
        key: (block["_category"], _entity(core, key)["category"])
        for key, block in core["spec"].items()
        if block["_category"] != _entity(core, key)["category"]
    }
    assert not misfiled, (
        f"{core['id']}: filed under the wrong category (spec, manifest): {misfiled}. Move "
        f"the block — or change `category=` in the adapter, if the adapter is the wrong one."
    )


def test_the_label_matches_the_core(core):
    """The label exists so a reviewer reading `crm → Correspondence` knows what that is
    without opening Python. Because it is checked it cannot lie, and because it is
    checked it is not a second source of truth."""
    wrong = {
        key: (block.get("label"), _entity(core, key)["label"])
        for key, block in core["spec"].items()
        if block.get("label") != _entity(core, key)["label"]
    }
    assert not wrong, f"{core['id']}: label drift (spec, core): {wrong}"


def test_entities_are_ordered_the_way_the_review_sheet_reads_them(core):
    """Same sequence as `review.xlsx`, so someone moving between the two is at the same
    place in both (`cores/<id>/order.py`).

    Not tidiness for its own sake: 50 blocks edited by hand over months land wherever
    the last diff put them, and "where is GoodsReceipt" then costs a search instead of a
    glance.
    """
    order = _chain_order(core["id"])
    if order is None:  # a core without the shared sequence states no order to check
        return
    for category, entities in core["categories"].items():
        actual = list(entities)
        expected = order(actual)
        assert actual == expected, (
            f"{core['id']}: {category} is out of order. Expected:\n  " + "\n  ".join(expected)
        )


def test_a_duplicate_entity_key_is_refused():
    """`safe_load` lets the second win silently. In a 1 700-line file with 50 sibling
    blocks that would delete an entity's whole specification and still parse clean."""
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        # S506 reads a custom loader as unsafe; this one subclasses SafeLoader and only
        # adds the duplicate-key refusal, so it constructs exactly what safe_load does.
        yaml.load("documents:\n  Quote: {label: a}\n  Quote: {label: b}\n", StrictLoader)  # noqa: S506
