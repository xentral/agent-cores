"""A return's lifecycle is filterable again — as `progress`, not as `status`.

`status` merges two upstream fields: `cancelled` is the document status, the
other four mirror `progress` (announced | received | checked | done). The filter
map sent DOCUMENT-status values against the `status` key, so it filtered the
wrong field: measured on mvp, `checked` returned 32 records that were all
`settled`, and `requested` returned 47 of mixed progress.

Aliasing `status` to `progress` would have fixed those four and broken
`cancelled` — the one value that worked — because `progress` has no such value.
So the lifecycle becomes its own field, and `status` keeps only the value it can
actually answer.

Verified against mvp after the split: requested -> 30 announced, received -> 10,
checked -> 7, settled -> 35 done, and status=cancelled -> 3.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import (
    _PROGRESS,
    _PROGRESS_OPTIONS,
    ReturnAdapter,
)


def _adapter() -> ReturnAdapter:
    return ReturnAdapter.__new__(ReturnAdapter)


def _wire(model_value: str) -> str:
    maps = _adapter().filter_value_maps.get("progress") or {}
    return maps.get(model_value, model_value)


@pytest.mark.parametrize(
    ("model", "upstream"),
    [
        ("requested", "announced"),
        ("received", "received"),
        ("checked", "checked"),
        ("settled", "done"),
    ],
)
def test_each_progress_value_maps_to_the_upstream_spelling(model, upstream):
    assert _wire(model) == upstream


def test_the_filter_and_the_read_are_inverses():
    """The bug was exactly this: filtering by X returned records reading as Y."""
    for option in _PROGRESS_OPTIONS:
        model = option["value"]
        assert _PROGRESS[_wire(model)] == model, f"{model} does not round-trip"


def test_progress_is_filterable_and_offers_only_the_four():
    """`cancelled` is deliberately absent — it is not a progress value, and
    offering it would filter `progress=cancelled`, which upstream rejects."""
    field = _adapter().fields()["progress"]
    assert field.get("filterable") is True
    assert [o["value"] for o in field["options"]] == [
        "requested",
        "received",
        "checked",
        "settled",
    ]


def test_status_no_longer_carries_a_filter_map():
    """It used to map four values onto document-status spellings, which is what
    made it filter the wrong field. Its only filterable value, `cancelled`, is
    already the upstream spelling and needs no entry."""
    assert "status" not in _adapter().filter_value_maps


def test_status_still_reads_the_merged_lifecycle():
    """The read shape does not change — only what can be filtered does."""
    adapter = _adapter()
    assert adapter.map_read({"id": 1, "status": "cancelled", "progress": "done"})["status"] == (
        "cancelled"
    )
    merged = adapter.map_read({"id": 1, "status": "completed", "progress": "done"})
    assert merged["status"] == "settled"
    assert merged["progress"] == "settled"
