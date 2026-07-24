#!/usr/bin/env python3
"""Structure + manifest validation for agent-cores.

Runs locally and in CI. Does NOT import the cores (that needs the backend
`entity_registry.core_sdk` contract — see the import-smoke CI job); this checks
only the repo's own consistency:

  * manifest.json has the required shape and a sane contractVersion,
  * every `cores/<id>/` is an importable package (has __init__.py),
  * the set of core folders matches manifest.cores.ids exactly.

Exit non-zero on any violation so CI fails the PR.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORES_DIR = ROOT / "cores"
MANIFEST = ROOT / "manifest.json"

REQUIRED_MANIFEST_KEYS = {"schemaVersion", "version", "contractVersion", "cores"}


def _fail(msg: str) -> None:
    print(f"::error::{msg}")


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

    if errors:
        print(f"validate_cores: {errors} error(s)")
        return 1
    print(
        f"validate_cores: OK ({len(on_disk)} core(s), contractVersion={manifest['contractVersion']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
