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
import asyncio
import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter
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


# ---- native upstream search vs the emulated fan-out -----------------------


def _capture(adapter, term="Lily"):
    """Run a consolidated search and return the query/queries that reached the wire."""
    seen: list[list[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(list(request.url.params.multi_items()))
        return httpx.Response(200, json={"data": [], "meta": {"total": 0}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.request(
                method="GET",
                handle=None,
                query=[
                    ("page[size]", "5"),
                    ("filter[0][key]", "search"),
                    ("filter[0][op]", "contains"),
                    ("filter[0][value]", term),
                ],
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    asyncio.run(go())
    return seen


def test_a_document_hands_the_term_to_the_upstreams_own_search():
    """v3 searches the nine document endpoints itself, across 9-14 columns. Emulating
    that with a fan-out over our single `searchable` flag was strictly worse: measured
    on mvp, a sales-order search for the customer's name found 0 through the fan-out
    and 14 natively."""
    seen = _capture(SalesOrderAdapter())
    assert len(seen) == 1, f"expected ONE upstream call, got {len(seen)}"
    params = dict(seen[0])
    assert params.get("search") == "Lily"
    assert not any(k.startswith("filter[") for k in params), (
        f"the search filter group must not travel on as a filter: {params}"
    )


def test_a_partner_still_fans_out_because_upstream_would_ignore_the_term():
    """`?search=` on customers is not refused, it is SILENTLY IGNORED — measured:
    `?search=nonsense` answers 200 with all 20128 rows. So the emulation stays, and
    one request per searchable field is what correctness costs here."""
    seen = _capture(CustomerAdapter())
    assert len(seen) == len(CustomerAdapter().search_fields()) > 1
    for params in (dict(q) for q in seen):
        assert "search" not in params, "a bare ?search= would be ignored and return everything"
        assert any(k.endswith("][key]") for k in params)


def test_a_document_advertises_what_the_native_search_matches():
    """`searchFields` is what consumers key their search affordance off. The schema
    flags only `number` on a document, so advertising those would understate the
    native reach by five fields."""
    fields = SalesOrderAdapter().metadata()["searchFields"]
    assert "billingAddress.name" in fields
    assert "references.customerOrderNumber" in fields
    assert set(SalesOrderAdapter().search_fields()) == {"number"}


def test_stripping_the_search_group_leaves_other_filters_alone():
    from xentral_entity_cores.xentral_api.emulated._search import strip_search

    query = [
        ("page[size]", "5"),
        ("filter[0][key]", "status"),
        ("filter[0][value]", "open"),
        ("filter[1][key]", "search"),
        ("filter[1][value]", "Lily"),
    ]
    kept = dict(strip_search(query))
    assert kept["filter[0][key]"] == "status"
    assert not any(v == "search" for v in kept.values())
