"""Consolidated search metadata + ONE list total in both envelope places.

The Neo core previously advertised no ``searchFields`` (record pickers fell
back to client-side first-page filtering) and dropped the upstream ``meta``
block from list envelopes — consumers reading ``meta.total`` saw nothing
while ``extra.total`` said 27, the classic overview-vs-table count mismatch.

``searchFields`` now derives from the per-field ``searchable`` flags, and
``_list_envelope`` emits ``meta.total``/``extra.total`` from the same source
plus the ``page/perPage/lastPage`` paging block.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter


def test_search_fields_derive_from_searchable_flags():
    assert CreditNoteAdapter().search_fields() == ("number",)
    assert set(CustomerAdapter().search_fields()) == {"number", "name", "email"}


def test_metadata_advertises_search_fields():
    meta = CustomerAdapter().metadata()
    assert set(meta["searchFields"]) == {"number", "name", "email"}


def test_list_envelope_v3_meta_total_lands_in_both_places():
    body = CreditNoteAdapter()._list_envelope(
        [{"id": "cn_1"}],
        {"meta": {"total": 27}},
        [("page[number]", "1"), ("page[size]", "25")],
    )
    assert body["meta"]["total"] == 27
    assert body["extra"]["total"] == 27
    assert body["meta"]["lastPage"] == 2
    assert body["meta"]["page"] == 1
    assert body["meta"]["perPage"] == 25


def test_list_envelope_v1_total_count_lands_in_both_places():
    body = CreditNoteAdapter()._list_envelope(
        [],
        {"extra": {"totalCount": 60}},
        [("page[number]", "3"), ("page[size]", "20")],
    )
    assert body["meta"]["total"] == 60
    assert body["extra"]["total"] == 60
    assert body["meta"]["lastPage"] == 3
    assert body["meta"]["page"] == 3


def test_list_envelope_without_total_keeps_paging_only():
    body = CreditNoteAdapter()._list_envelope([], {}, [])
    assert "total" not in body["meta"]
    assert "total" not in body["extra"]
    assert body["meta"]["page"] == 1
    assert body["meta"]["perPage"] == 25
