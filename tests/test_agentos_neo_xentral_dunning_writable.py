"""The dunning level is writable — the core said it was not.

`dunning.level` was declared `integer` and read-only. Both are wrong, and the
schema contradicted itself: the read passed the upstream STRING straight through
on every record, so the declared type never matched what came back.

Upstream `dunningSettings` sits in `UpdateInvoiceData` (documents spec) with
`level` as a string enum, plus `blocked` and `comment`. A PATCH can set it.

`dunning` was also in neither `_WRITABLE` nor `_IGNORE`, so a write naming it was
rejected outright — while the schema advertised `blocked` and `note` as writable.
Declared writable, actually refused.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import (
    _DUNNING_LEVELS,
    SalesInvoiceAdapter,
)


def _adapter() -> SalesInvoiceAdapter:
    return SalesInvoiceAdapter.__new__(SalesInvoiceAdapter)


def _level_prop() -> dict:
    return _adapter().fields()["dunning"]["properties"]["level"]


def test_the_level_is_writable():
    prop = _level_prop()
    assert prop.get("access") != "readOnly"
    assert prop.get("creatable") and prop.get("updatable")


def test_the_level_offers_the_upstream_vocabulary():
    """An integer would have been unusable — upstream takes these strings."""
    assert [o["value"] for o in _level_prop()["options"]] == list(_DUNNING_LEVELS)


def test_the_declared_type_matches_what_the_read_returns():
    """The old declaration said integer while the read returned the string."""
    record = _adapter().map_read({"id": 1, "dunningSettings": {"level": "reminder1"}})
    assert record["dunning"]["level"] in _DUNNING_LEVELS
    assert _level_prop()["type"] == "select"


@pytest.mark.parametrize("level", _DUNNING_LEVELS)
def test_every_level_reaches_upstream_unchanged(level):
    body, rejected = _adapter().map_write({"dunning": {"level": level}}, creating=False)
    assert not rejected
    assert body["dunningSettings"] == {"level": level}


def test_note_is_sent_as_the_upstream_comment():
    """The model calls it `note`, upstream calls it `comment` — the read already
    mapped it, the write did not exist at all."""
    body, rejected = _adapter().map_write(
        {"dunning": {"blocked": True, "note": "Kunde hat Ratenzahlung"}}, creating=False
    )
    assert not rejected
    assert body["dunningSettings"] == {"blocked": True, "comment": "Kunde hat Ratenzahlung"}


def test_a_write_naming_dunning_is_no_longer_rejected():
    """It was in neither _WRITABLE nor _IGNORE, so it earned a 409 — for fields
    the schema itself advertised as writable."""
    _body, rejected = _adapter().map_write({"dunning": {"blocked": False}}, creating=False)
    assert "dunning" not in rejected


def test_the_read_shape_is_unchanged():
    record = _adapter().map_read(
        {"id": 1, "dunningSettings": {"level": "reminder2", "blocked": True, "comment": "x"}}
    )
    assert record["dunning"] == {
        "level": "reminder2",
        "blocked": True,
        "lastReminderAt": None,
        "note": "x",
    }
