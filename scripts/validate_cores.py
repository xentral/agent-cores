#!/usr/bin/env python3
"""Structure + manifest validation for agent-cores.

Runs locally and in CI. Does NOT import the cores (that needs the backend
`entity_registry.core_sdk` contract — see the import-smoke CI job); this checks
only the repo's own consistency:

  * manifest.json has the required shape and a sane contractVersion,
  * every `cores/<id>/` is an importable package (has __init__.py),
  * the set of core folders matches manifest.cores.ids exactly,
  * every verdict in a `verified.json` is in the vocabulary (cores/agentos_neo_xentral/verdicts.py).

Exit non-zero on any violation so CI fails the PR.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORES_DIR = ROOT / "cores"
MANIFEST = ROOT / "manifest.json"

REQUIRED_MANIFEST_KEYS = {"schemaVersion", "version", "contractVersion", "cores"}

# The verdict vocabulary, loaded straight from the file rather than imported as a
# module: this script deliberately does not import the cores (that needs the
# backend contract), but the vocabulary must still exist in exactly one place —
# restating it here is how the two would drift.
_VERDICTS_SRC = CORES_DIR / "agentos_neo_xentral" / "verdicts.py"


def _verdict_vocabulary() -> set[str]:
    spec = importlib.util.spec_from_file_location("_verdicts", _VERDICTS_SRC)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"cannot load {_VERDICTS_SRC}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.VERDICTS)


def _fail(msg: str) -> None:
    print(f"::error::{msg}")


def _verdicts_in(entity: dict) -> list[str]:
    """Every verdict an entity block holds.

    Read from the named places rather than by walking: an entity also carries
    timestamps (``probedAt``) and free-text notes keyed by action key
    (``actionsNotes``), and a blind walk reports those as verdicts. ``operations``
    covers the older ``xentral_api`` manifest shape, which uses the same words.
    """
    out: list[str] = []
    for facets in (entity.get("fields") or {}).values():
        if isinstance(facets, dict):
            out += [v for k, v in facets.items() if isinstance(v, str) and not k.endswith("Note")]
    for group in ("operations", "actions", "processSteps"):
        values = entity.get(group)
        if isinstance(values, dict):
            out += [v for v in values.values() if isinstance(v, str)]
    return out


def _check_verdicts(core_id: str) -> int:
    """Refuse a verdict string outside the vocabulary.

    The point is the retired word ``pass`` on a facet that cannot earn it: the file
    used to say ``pass`` for a read nobody had observed and for an action that only
    proved its route exists. A writer that silently falls back to the old vocabulary
    would otherwise reintroduce exactly that, and no reader could tell.
    """
    path = CORES_DIR / core_id / "verified.json"
    if not path.is_file():
        return 0
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"cores/{core_id}/verified.json unreadable: {exc}")
        return 1
    allowed = _verdict_vocabulary()
    bad = sorted(
        {
            v
            for entity in (manifest.get("entities") or {}).values()
            if isinstance(entity, dict)
            for v in _verdicts_in(entity)
            if v not in allowed
        }
    )
    if not bad:
        return 0
    _fail(
        f"cores/{core_id}/verified.json: verdict(s) outside the vocabulary: {bad} "
        f"(allowed: {sorted(allowed)} — see cores/agentos_neo_xentral/verdicts.py)"
    )
    return 1


def main() -> int:
    errors = 0

    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as exc:
        _fail(f"manifest.json unreadable: {exc}")
        return 1

    missing = REQUIRED_MANIFEST_KEYS - manifest.keys()
    if missing:
        _fail(f"manifest.json missing keys: {sorted(missing)}")
        errors += 1

    if not isinstance(manifest.get("contractVersion"), int):
        _fail("manifest.json: contractVersion must be an integer")
        errors += 1

    declared = manifest.get("cores") or {}
    declared_ids = set(declared.get("ids") or [])
    if declared.get("count") != len(declared_ids):
        _fail(f"manifest cores.count ({declared.get('count')}) != len(ids) ({len(declared_ids)})")
        errors += 1

    # Folders on disk (a core is a directory with an __init__.py).
    on_disk = {
        p.name for p in CORES_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))
    }

    if declared_ids - on_disk:
        _fail(f"manifest lists cores with no folder: {sorted(declared_ids - on_disk)}")
        errors += 1
    if on_disk - declared_ids:
        _fail(f"core folders missing from manifest.cores.ids: {sorted(on_disk - declared_ids)}")
        errors += 1

    for core_id in sorted(on_disk):
        if not (CORES_DIR / core_id / "__init__.py").is_file():
            _fail(f"cores/{core_id} has no __init__.py (must export CORE)")
            errors += 1
        errors += _check_verdicts(core_id)

    if errors:
        print(f"validate_cores: {errors} error(s)")
        return 1
    print(
        f"validate_cores: OK ({len(on_disk)} core(s), contractVersion={manifest['contractVersion']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
