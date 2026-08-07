"""Filtering a delivery note by `shipped` returned the delivered ones.

The read map and the filter map disagreed. `_STATUS` knew that upstream says
`sent` for versendet — that was fixed once, with a comment counting 44 of mvp's
100 notes — but `filter_value_maps` was left pointing `shipped` at `completed`,
and `completed` reads back as `delivered`. So the one value a shipping workflow
actually asks for returned a different state, silently.

Upstream vocabulary, measured on mvp: draft | released | sent | completed |
cancelled. `shipped` and `delivered` are model-only names and are REJECTED with a
400, so the map is the only thing that can bridge them.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter

_UPSTREAM = {"draft", "released", "sent", "completed", "cancelled"}


def _wire(model_value: str) -> str:
    adapter = DeliveryNoteAdapter.__new__(DeliveryNoteAdapter)
    return (adapter.filter_value_maps.get("status") or {}).get(model_value, model_value)


@pytest.mark.parametrize(
    ("model", "upstream"),
    [
        ("draft", "draft"),
        ("picking", "released"),
        ("shipped", "sent"),
        ("delivered", "completed"),
        ("cancelled", "cancelled"),
    ],
)
def test_every_model_status_maps_to_the_value_upstream_accepts(model, upstream):
    assert _wire(model) == upstream


def test_no_model_status_reaches_upstream_as_a_value_it_rejects():
    """`shipped`/`delivered` earn a 400 upstream — a map entry that let one
    through would turn a silent wrong result into a broken list."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import _STATUS_OPTIONS

    for option in _STATUS_OPTIONS:
        assert _wire(option["value"]) in _UPSTREAM


def test_the_filter_and_the_read_are_inverses():
    """The bug was exactly this: filtering by X returned records reading as Y."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import (
        _STATUS,
        _STATUS_OPTIONS,
    )

    for option in _STATUS_OPTIONS:
        model = option["value"]
        assert _STATUS[_wire(model)] == model, f"{model} does not round-trip"
