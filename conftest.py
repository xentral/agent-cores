"""Test setup: make the cores importable the same way the backend does.

The backend imports vendored cores under a synthetic package
``xentral_entity_cores.<id>`` (never top-level — a core id like ``xentral_api``
would otherwise shadow a same-named backend module). Tests here mirror that: they
import ``from xentral_entity_cores.<core>.…``, so this registers the synthetic
package pointing at this repo's ``cores/`` dir.

Running the tests also needs the backend contract (``entity_registry.core_sdk``)
and, for some tests, ``mcp_server`` — put the agent-hub-labs ``backend/`` on
PYTHONPATH (the CI import-smoke job checks it out; locally point it at your
checkout).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_PKG = "xentral_entity_cores"
_CORES = Path(__file__).parent / "cores"

_pkg = sys.modules.get(_PKG)
if _pkg is None:
    _pkg = types.ModuleType(_PKG)
    _pkg.__package__ = _PKG
    sys.modules[_PKG] = _pkg
_pkg.__path__ = [str(_CORES)]  # type: ignore[attr-defined]
