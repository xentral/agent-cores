"""Every core: a write whose semantics cannot be read off the schema must say them.

An agent plans from `describe`. When the rule lives in a docstring or a code
comment, the agent does not have it — and the failure is silent, because the
call succeeds and does the wrong thing.

This happened with `Product.bom.items`: sending a list REPLACES the bill of
materials, so an agent changing one component's quantity by sending that one
line silently deleted the others. The response to a truncating write was 200.

Two shapes are ambiguous *by construction* — no naming or typing can resolve
them, so only prose can:

  * a writable COLLECTION — does sending a list replace, append, or merge?
  * an executable ACTION with a command — what does invoking it actually do?

The collection check looks for a word that NAMES the effect on existing rows,
not merely for the presence of a description. That distinction is the whole
point: `bom.items` did carry a description — "Component lines making up the
product." — and it was exactly as dangerous as carrying none, because it
described the rows instead of the write. A first draft of this file checked only
for presence and passed the very defect it was written for.

A keyword check cannot verify a claim is TRUE; someone can paste "replaces"
onto a collection that appends. It converts an omission into a deliberate act,
which is the reachable goal — the failure being prevented is nobody thinking
about it, not somebody lying.

Enacting this surfaced eight further collections (customer/supplier addresses
and contacts, in two cores) that described their rows and not their write
semantics. Both cores turned out to replace, by different mechanics: the Xentral
core upserts by `id` and deletes omitted entries, the Postgres core overwrites
the array wholesale via a shallow JSONB merge. Each now says its own.
"""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import pytest

_PKG = "xentral_entity_cores"
_CORES = Path(__file__).resolve().parent.parent / "cores"


def _core_ids() -> list[str]:
    return sorted(p.name for p in _CORES.iterdir() if p.is_dir() and (p / "__init__.py").is_file())


def _adapters(core_id: str) -> list[Any]:
    pkg = sys.modules.get(_PKG)
    if pkg is None:
        pkg = types.ModuleType(_PKG)
        pkg.__package__ = _PKG
        sys.modules[_PKG] = pkg
    pkg.__path__ = [str(_CORES)]  # type: ignore[attr-defined]
    core = getattr(importlib.import_module(f"{_PKG}.{core_id}"), "CORE", None)
    if core is None:
        return []
    out = []
    for a in core.resolve_adapters():
        out.append(a() if isinstance(a, type) else a)
    return out


def _walk(props: dict[str, Any] | None, prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}.{name}" if prefix else name
        yield path, spec
        sub = spec.get("properties")
        if not isinstance(sub, dict):
            sub = (spec.get("node") or {}).get("properties")
        if isinstance(sub, dict):
            yield from _walk(sub, path)


def _metadata(adapter: Any) -> dict[str, Any] | None:
    try:
        return adapter.metadata("en")
    except Exception:  # noqa: BLE001 - a broken adapter is another test's problem
        return None


# Words that name what happens to the EXISTING rows when a list is written.
# Presence of *a* description is not enough: `bom.items` carried "Component
# lines making up the product." and was exactly as dangerous as carrying none.
# This does not verify the claim is true — nothing here can. It forces the
# author to make the choice explicitly instead of silently not considering it.
_WRITE_SEMANTICS = re.compile(r"replac|append|merge|clear|overwrit|supersede", re.I)


@pytest.mark.parametrize("core_id", _core_ids())
def test_writable_collections_state_their_write_semantics(core_id: str):
    """Sending a list can replace, append or merge. The schema cannot say which,
    so the description must — otherwise the first caller finds out by losing
    data, on a call that answered 200."""
    offenders: list[str] = []
    for adapter in _adapters(core_id):
        meta = _metadata(adapter)
        if not meta:
            continue
        for path, spec in _walk((meta.get("rootNode") or {}).get("properties")):
            writable = spec.get("creatable") or spec.get("updatable")
            if spec.get("type") != "collection" or not writable:
                continue
            if not _WRITE_SEMANTICS.search(spec.get("description") or ""):
                offenders.append(f"{adapter.manifest.key}.{path}")
    assert not offenders, (
        f"{core_id}: writable collection(s) whose description does not say what "
        f"writing does to the existing rows: {offenders}. State whether sending a "
        "list REPLACES the collection, appends to it, or merges by id — a caller "
        "cannot tell from the schema, and guessing wrong deletes rows on a call "
        "that answers 200. Describing the CONTENT of the rows is not enough."
    )


@pytest.mark.parametrize("core_id", _core_ids())
def test_executable_actions_say_what_they_do(core_id: str):
    """An action with a command takes arguments and has an effect. Its key is a
    label, not a specification. A `wish` is exempt: it is not executable, and its
    reason already explains itself."""
    offenders: list[str] = []
    for adapter in _adapters(core_id):
        meta = _metadata(adapter)
        if not meta:
            continue
        for action in meta.get("actions") or []:
            if not isinstance(action, dict):
                continue
            if action.get("command") and not action.get("wish") and not action.get("description"):
                offenders.append(f"{adapter.manifest.key}.{action.get('key')}")
    assert not offenders, (
        f"{core_id}: executable action(s) with a command but no description: "
        f"{offenders}. The key names the action; only the description can say "
        "what it does to the record and what the arguments mean."
    )


def test_the_guard_rejects_a_description_that_only_says_what_the_rows_are():
    """A conformance test that cannot fail is decoration — and this one nearly
    was. The first version only checked that a description EXISTED, which the
    real defect would have passed: `bom.items` carried "Component lines making
    up the product." from the descriptions overlay while silently replacing the
    whole bill. The verbatim text is used here so the regression that motivated
    the check is the thing being asserted against."""
    innocuous = "Component lines making up the product."
    honest = "REPLACES the whole bill of materials on a create/update — not an append."
    assert not _WRITE_SEMANTICS.search(innocuous)
    assert _WRITE_SEMANTICS.search(honest)


def test_the_guard_flags_a_collection_with_no_description_at_all():
    bare = {"type": "collection", "creatable": True, "node": {"properties": {}}}
    found = [
        path
        for path, spec in _walk({"items": bare})
        if spec.get("type") == "collection"
        and (spec.get("creatable") or spec.get("updatable"))
        and not _WRITE_SEMANTICS.search(spec.get("description") or "")
    ]
    assert found == ["items"]
