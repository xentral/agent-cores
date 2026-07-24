"""Import-smoke: every core in this repo loads against the live backend contract.

Runs in CI with the agent-hub-labs backend importable (PYTHONPATH) and
``XENTRAL_CORES_ROOT`` pointing at this repo's ``cores/``. Importing the registry
discovers every core under the ``xentral_entity_cores`` namespace, validates each
``CORE`` against ``entity_registry.core_sdk`` + its declared ``CONTRACT_VERSION``,
and fails loudly on any incompatibility — so a core edit that breaks the contract
(or a contract change the cores haven't caught up to) is caught here, before a
tag is cut and the backend is bumped.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_all_manifest_cores_load_against_the_contract() -> None:
    # Imports the backend registry, which runs discovery over XENTRAL_CORES_ROOT
    # (set to this repo's cores/ by CI) and validates each core.
    from entity_registry.cores.registry import list_cores

    loaded = {c.id for c in list_cores()}
    declared = set(json.loads((_REPO / "manifest.json").read_text())["cores"]["ids"])

    missing = declared - loaded
    assert not missing, f"cores declared in manifest.json did not load: {sorted(missing)}"
    assert loaded, "no cores loaded"
