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
    assert set(CustomerAdapter().search_fields()) == {
        "number",
        "name",
        "email",
        "addresses.street",
        "addresses.zip",
        "addresses.city",
    }


def test_a_low_cardinality_key_is_filterable_but_not_searchable():
    """`state`/`country` answer a precise question well and a free-text one badly:
    the fan-out ORs one `contains` request per field and merges the first page of
    each, so a two-letter country code returns the first page of every German
    record and pushes the actual match out. Measured on mvp — the same search found
    its record on Supplier (32 rows) and lost it on Customer (20128)."""
    props = CustomerAdapter().fields()["addresses"]["node"]["properties"]
    for leaf in ("state", "country"):
        assert props[leaf]["filterable"] is True
        assert not props[leaf].get("searchable")


def test_a_nested_searchable_leaf_reaches_the_fan_out():
    """The walk used to stop at the top level, so five address leaves the schema
    advertises as searchable were never searched — and on PurchaseInvoice, whose
    only searchable field is nested, the fan-out was empty and search did nothing
    at all. Measured on mvp before switching this on: each of them answers a
    `contains` filter with 200 and narrows (customers 20128 -> 8 for a city)."""
    for path in ("addresses.city", "addresses.zip", "addresses.street"):
        assert path in CustomerAdapter().search_fields()


def test_metadata_advertises_search_fields():
    """`searchFields` is the contract consumers key their record pickers on, so the
    nested paths have to be visible there too, not just used internally."""
    meta = CustomerAdapter().metadata()
    assert set(meta["searchFields"]) == set(CustomerAdapter().search_fields())
    assert "addresses.city" in meta["searchFields"]


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


def test_clamped_page_size_is_reported_not_the_request():
    """v1 caps perPage at 50 — lastPage must follow what was served.

    The mvp tenant's sales prices: asking for 100 got 50, and reporting the
    request made lastPage 613 instead of 1226, hiding half the rows behind a
    number that looks authoritative.
    """
    body = CreditNoteAdapter()._list_envelope(
        [],
        {"extra": {"totalCount": 61256, "page": {"number": 1, "size": 50}}},
        [("page[number]", "1"), ("page[size]", "100")],
    )
    assert body["meta"]["perPage"] == 50
    assert body["meta"]["lastPage"] == 1226


def test_v3_meta_per_page_echo_wins_over_request():
    body = CreditNoteAdapter()._list_envelope(
        [],
        {"meta": {"total": 300, "perPage": 50}},
        [("page[number]", "1"), ("page[size]", "100")],
    )
    assert body["meta"]["perPage"] == 50
    assert body["meta"]["lastPage"] == 6


def test_unclamped_echo_leaves_the_request_intact():
    body = CreditNoteAdapter()._list_envelope(
        [],
        {"extra": {"totalCount": 300, "page": {"number": 1, "size": 25}}},
        [("page[number]", "1"), ("page[size]", "25")],
    )
    assert body["meta"]["perPage"] == 25
    assert body["meta"]["lastPage"] == 12


def test_absent_echo_falls_back_to_the_request():
    """No echo is not evidence of a clamp — a short final page is normal."""
    body = CreditNoteAdapter()._list_envelope(
        [{"id": "cn_1"}],
        {"meta": {"total": 27}},
        [("page[number]", "2"), ("page[size]", "25")],
    )
    assert body["meta"]["perPage"] == 25
    assert body["meta"]["lastPage"] == 2


def test_malformed_echo_is_ignored():
    for bogus in ({"size": 0}, {"size": "50"}, {"size": None}, {"size": True}, "nonsense"):
        body = CreditNoteAdapter()._list_envelope(
            [],
            {"extra": {"totalCount": 100, "page": bogus}},
            [("page[number]", "1"), ("page[size]", "25")],
        )
        assert body["meta"]["perPage"] == 25, bogus
        assert body["meta"]["lastPage"] == 4, bogus
