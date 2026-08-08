"""The checked-in review sheet must still match its sources.

``review.yaml`` is generated from three files that change for different reasons: the
specification (a domain decision), the adapters (a build), and ``verified.json`` (a
probe run). A sheet that silently lags behind any of them is worse than none — it is
the artefact a reviewer signs, and it would be showing them last month's system while
claiming to show this one. That is the exact failure mode the whole spec arrangement
exists to prevent, so it gets the same treatment: a machine notices instead of a
person remembering.

Only the YAML half is checked. The workbook is the same data rendered for reading,
and it cannot be compared byte-wise anyway — an xlsx is a zip and carries its own
creation time, so a re-render always differs. ``export_review_sheet`` keeps openpyxl
out of module scope precisely so this test can rebuild the YAML without it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "export_review_sheet.py"
_CORES = _ROOT / "cores"

CORES_WITH_A_SHEET = sorted(p.parent.name for p in _CORES.glob("*/review.yaml"))


def _exporter():
    """Load the script by path — `scripts/` is a directory of entry points, not a
    package, so there is nothing to import by name."""
    spec = importlib.util.spec_from_file_location("export_review_sheet", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_review_sheet"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("core_id", CORES_WITH_A_SHEET)
def test_review_sheet_is_current(core_id):
    exporter = _exporter()
    source, summary, cap_rows, field_rows = exporter.build(core_id)
    expected = exporter.render_yaml(core_id, source, summary, cap_rows, field_rows)
    actual = (_CORES / core_id / "review.yaml").read_text("utf-8")
    assert actual == expected, (
        f"{core_id}: review.yaml no longer matches the specification, the core or "
        f"verified.json. Regenerate it:\n"
        f"  PYTHONPATH=<agent-os>/backend uv run --project <agent-os>/backend "
        f"--with openpyxl --with pyyaml python scripts/export_review_sheet.py {core_id}"
    )


def test_a_sheet_exists_to_check():
    """Guards the discovery: renaming the output would otherwise turn this module into
    a silent no-op that still reports green."""
    assert CORES_WITH_A_SHEET, "no core ships a review.yaml"
