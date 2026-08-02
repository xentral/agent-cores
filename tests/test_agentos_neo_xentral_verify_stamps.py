"""The manifest says when it was measured, per entity.

`verified.json` is assembled from many scoped runs, so it has no single age: the
seven sales documents were re-probed weeks after the rest, and reading the
workbook you could not tell. `generatedAt` was written as `None` on every run, so
the exporter fell back to the file's mtime — which after a fresh clone is the
checkout time, i.e. a number that looks authoritative and means nothing.

Now the run stamps itself and every entity block, and action verdicts carry their
own stamp because a narrow run (`VERIFY_ACTIONS_ONLY`) refreshes them
independently of the fields.
"""

from __future__ import annotations

import datetime as dt
import re

from xentral_entity_cores.agentos_neo_xentral.checks import verify

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def test_the_stamp_is_utc_to_the_second() -> None:
    """One shape everywhere, so the exporter can parse it without guessing."""
    assert _ISO.match(verify._stamp())


def test_it_is_now() -> None:
    made = dt.datetime.fromisoformat(verify._stamp())
    assert abs((dt.datetime.now(dt.UTC) - made).total_seconds()) < 5


def test_the_run_no_longer_writes_a_null_generated_at() -> None:
    """The regression: `{"generatedAt": None}` on every single run."""
    import inspect

    source = inspect.getsource(verify._main)
    assert '"generatedAt": None' not in source
    assert '"generatedAt": _stamp()' in source


def test_each_entity_block_is_stamped() -> None:
    import inspect

    assert '"probedAt": _stamp()' in inspect.getsource(verify._verify_entity)


def test_action_verdicts_carry_their_own_stamp() -> None:
    """Fields and actions are refreshed by different runs — one date for both would
    date the actions to whenever the fields were last touched."""
    import inspect

    entity = inspect.getsource(verify._verify_entity)
    assert '"actionsProbedAt"' in entity
    main = inspect.getsource(verify._main)
    assert '"actionsProbedAt" in before' in main, "a carried-over verdict loses its date"


def test_the_committed_manifest_carries_the_stamps() -> None:
    """Not just the code path: the shipped file has them, so the workbook can show
    per-entity ages instead of one mtime for all 47."""
    import json
    import pathlib

    path = pathlib.Path(verify.__file__).resolve().parent.parent / "verified.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("generatedAt"), "manifest predates the stamping — re-run verify"
    entities = data["entities"]
    unstamped = sorted(k for k, v in entities.items() if not (v or {}).get("probedAt"))
    assert unstamped == [], unstamped
