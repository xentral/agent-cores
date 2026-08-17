"""An unfiltered document list must contain the drafts.

Every v3 document controller declares its status filter as
``QueryFilter::string('status', …)->default([...])``, and every one of those
defaults omits ``draft``. So a list call that names no status silently dropped
every draft — measured on mvp: 10 rows for a customer, 4 more only once ``draft``
was named. A fresh document is a draft AND has ``documentNumber: null``, so it was
missing from lists and unfindable by number: the single most expensive trap in
this core, and the thing ADR-007 ("Listen haben KEINE versteckten Defaults. Kein
impliziter Status-Filter") promised would not happen.

``FacadeAdapterBase.list_status_values`` is what makes the ADR true: the facade
names every status itself, so the upstream default never applies. These tests
pin the two ways that can go wrong — a value the upstream rejects (which would
400 the whole list, not just lose a row), and an injection that overrides what
the caller actually asked for.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from xentral_entity_cores.xentral_api.emulated._search import filter_groups

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

# The seven documents whose upstream list endpoint hides drafts by default.
DOCUMENTS = [
    QuoteAdapter,
    SalesOrderAdapter,
    SalesInvoiceAdapter,
    DeliveryNoteAdapter,
    CreditNoteAdapter,
    ReturnAdapter,
    PurchaseOrderAdapter,
]
_IDS = [c.__name__ for c in DOCUMENTS]


def _adapter(cls):
    """An adapter without running __init__ — these tests read class attributes and
    call the pure query-shaping helpers, never the network."""
    return cls.__new__(cls)


@pytest.mark.parametrize("cls", DOCUMENTS, ids=_IDS)
def test_every_document_names_its_statuses(cls):
    """An empty tuple means the upstream default wins and drafts vanish again."""
    assert _adapter(cls).list_status_values, (
        f"{cls.__name__} would inherit the draft-hiding default"
    )


@pytest.mark.parametrize("cls", DOCUMENTS, ids=_IDS)
def test_draft_is_among_them(cls):
    """The whole point. Everything else here guards the fix; this IS the fix."""
    assert "draft" in _adapter(cls).list_status_values


# The measured upstream vocabulary per endpoint, transcribed from the field matrix
# (cores/agentos_neo_xentral/docs/artifacts/feldmatrix-v3-dokument-apis.html) and
# re-measured by backend/tools/draft_status_probe.py. Held here as a SECOND,
# independently sourced copy: the injected values bypass `filter_value_maps`, so
# one model-side name slipping into an adapter tuple would 400 the entire list
# rather than cost a row — DeliveryNote's `shipped`/`delivered` and SalesOrder's
# `confirmed` are the traps, and they all live in `_STATUS` looking respectable.
#
# Note that `sent` and `accepted` are legitimately BOTH: model names that map to
# something else on the way out (offers: sent→released, accepted→completed) AND
# upstream values in their own right. That collision is why this cannot be derived
# from `filter_value_maps` and has to be measured.
_UPSTREAM_ENUM = {
    "QuoteAdapter": {
        "draft",
        "released",
        "sent",
        "commissioned",
        "ordered",
        "accepted",
        "declined",
        "expired",
        "completed",
        "cancelled",
    },
    "SalesOrderAdapter": {"draft", "released", "sent", "completed", "cancelled"},
    "SalesInvoiceAdapter": {
        "draft",
        "released",
        "sent",
        "completed",
        "partiallyCancelled",
        "cancelled",
    },
    "DeliveryNoteAdapter": {"draft", "released", "sent", "completed", "cancelled"},
    "CreditNoteAdapter": {"draft", "released", "sent", "completed", "cancelled"},
    "ReturnAdapter": {"draft", "received", "released", "sent", "completed", "cancelled"},
    "PurchaseOrderAdapter": {"draft", "released", "sent", "completed", "cancelled"},
}


@pytest.mark.parametrize("cls", DOCUMENTS, ids=_IDS)
def test_the_requested_vocabulary_is_the_upstream_one_and_all_of_it(cls):
    """Equality, not containment, in both directions: a value upstream does not
    know 400s the whole list, and a value left out is a class of document that
    stays invisible — which is the bug being fixed."""
    assert set(_adapter(cls).list_status_values) == _UPSTREAM_ENUM[cls.__name__]


@pytest.mark.parametrize("cls", DOCUMENTS, ids=_IDS)
def test_every_value_maps_back_to_a_real_state(cls):
    """``status_map`` falls back to ``draft`` for a value its read map does not
    know — the bug that made 44 dispatched delivery notes read as Entwurf. Now
    that these statuses are actively requested, a gap here would not just mislabel
    an occasional row: it would mislabel a whole class of newly visible documents.

    Return is exempt: its model ``status`` is derived from ``progress``, not from
    this axis (see ``map_read``), so it has no ``_STATUS`` map to check against.
    """
    if cls is ReturnAdapter:
        pytest.skip("Return derives model status from `progress`, not from the status axis")
    module = __import__(cls.__module__, fromlist=["_STATUS"])
    read_map = module._STATUS
    unknown = [v for v in _adapter(cls).list_status_values if v not in read_map]
    assert not unknown, f"{cls.__name__} requests {unknown} but reads them as `draft`"


# ---- the injection itself -------------------------------------------------
#
# These drive the REAL ``_get`` over a mock transport and assert on the query it
# actually put on the wire. Re-deriving the params in the test instead would pass
# happily while ``_get`` did something else entirely.


def _drive(adapter_cls, query, *, handle=None, responses=None):
    """Run the real ``_get`` against a mock transport.

    Returns ``(captured requests, (status, payload))`` so a test can assert on the
    query that went out AND on what the caller got back."""
    seen: list[httpx.Request] = []
    queue = list(responses or [])

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if queue:
            return queue.pop(0)
        return httpx.Response(200, json={"data": [], "meta": {"total": 0}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    adapter = adapter_cls()

    async def go():
        try:
            return await adapter._get(
                "https://x.test",
                "t",
                handle=handle,
                query=list(query),
                accept_language=None,
                client=client,
            )
        finally:
            await client.aclose()

    return seen, asyncio.run(go())


def _call(adapter_cls, query, **kw):
    return _drive(adapter_cls, query, **kw)[0]


def _call_result(adapter_cls, query, **kw):
    return _drive(adapter_cls, query, **kw)[1]


def _status_params(request: httpx.Request) -> list[dict[str, str]]:
    """The status filter group(s) actually sent, read back off the URL."""
    pairs = list(request.url.params.multi_items())
    return [p for p in filter_groups(pairs).values() if p.get("key") == "status"]


def test_a_bare_list_gets_exactly_one_status_filter():
    sent = _call(SalesOrderAdapter, [("page[number]", "1"), ("page[size]", "25")])
    groups = _status_params(sent[0])
    assert len(groups) == 1
    assert groups[0]["op"] == "in"
    assert set(groups[0]["value"].split(",")) == set(SalesOrderAdapter.list_status_values)
    assert "draft" in groups[0]["value"].split(",")


def test_the_caller_wins():
    """`status=draft` must keep meaning `only drafts`. Injecting the full set on
    top would silently widen a deliberately narrow query — a worse bug than the
    one being fixed, because the caller asked explicitly."""
    query = [
        ("filter[0][key]", "status"),
        ("filter[0][op]", "equals"),
        ("filter[0][value]", "draft"),
    ]
    groups = _status_params(_call(SalesOrderAdapter, query)[0])
    assert len(groups) == 1
    assert groups[0] == {"key": "status", "op": "equals", "value": "draft"}


def test_a_mapped_caller_status_still_wins():
    """`filter_value_maps` turns the model's `fulfilled` into upstream `completed`
    BEFORE the injection point. The guard has to see the translated filter as the
    caller's, not mistake it for an absent one and widen the query back out."""
    query = [
        ("filter[0][key]", "status"),
        ("filter[0][op]", "equals"),
        ("filter[0][value]", "fulfilled"),
    ]
    groups = _status_params(_call(SalesOrderAdapter, query)[0])
    assert len(groups) == 1
    assert groups[0]["value"] == "completed"


def test_an_unrelated_filter_keeps_its_index():
    """The query builder indexes the caller's filters 0..n-1; ours must take the
    next free slot rather than colliding with (and overwriting half of) theirs."""
    query = [
        ("filter[0][key]", "customer"),
        ("filter[0][op]", "equals"),
        ("filter[0][value]", "20423"),
    ]
    params = list(_call(SalesOrderAdapter, query)[0].url.params.multi_items())
    groups = filter_groups(params)
    assert groups["filter[0]"]["key"] == "address.id"  # the caller's, aliased, intact
    assert groups["filter[0]"]["value"] == "20423"
    assert groups["filter[1]"]["key"] == "status"


def test_a_read_by_id_gets_none():
    """A single record has no list default to neutralise, and a filter on a detail
    route is meaningless at best."""
    assert _status_params(_call(SalesOrderAdapter, [], handle="so_4711")[0]) == []


def test_a_rejected_status_costs_the_filter_not_the_list():
    """A value this build has and the tenant's Xentral does not would otherwise
    400 EVERY list on the entity. Falling back leaves drafts hidden again — worse
    than the fix, better than an entity that cannot be listed at all."""
    refusal = httpx.Response(
        400, json={"message": "Invalid value: draft. Valid values are: released, sent"}
    )
    sent = _call(
        SalesOrderAdapter,
        [("page[size]", "25")],
        responses=[refusal, httpx.Response(200, json={"data": [], "meta": {"total": 0}})],
    )
    assert len(sent) == 2, "the injected filter should have been retried without"
    assert _status_params(sent[0])  # first attempt carried it
    assert _status_params(sent[1]) == []  # retry dropped it


def test_the_callers_own_400_still_reaches_them():
    """The retry drops only OUR filter, which cannot fix a caller's broken query —
    so their 400 must come back as a 400. Masking it as an empty 200 would be the
    worst outcome of all: a wrong answer that looks like a right one."""
    refusal = httpx.Response(400, json={"message": "Unknown filter key `nope`"})
    status, _ = _call_result(
        SalesOrderAdapter, [("page[size]", "25")], responses=[refusal, refusal]
    )
    assert status == 400


# ---- the Return exception -------------------------------------------------


def _return_status(record):
    adapter = _adapter(ReturnAdapter)
    return adapter.map_read(record)["status"]


def test_a_draft_return_reads_as_draft():
    """Return derives its model status from `progress`, which a draft carries like
    any other document (`announced`). Before drafts reached lists at all that was
    invisible; now it would announce an unreleased return as `requested` — a state
    it is not in, hiding that it still needs releasing."""
    assert _return_status({"id": 1, "status": "draft", "progress": "announced"}) == "draft"


def test_a_released_return_still_follows_progress():
    """The document axis must not swallow the progress chain it normally shows."""
    assert _return_status({"id": 1, "status": "released", "progress": "done"}) == "settled"


def test_a_cancelled_return_still_reads_as_cancelled():
    assert _return_status({"id": 1, "status": "cancelled", "progress": "announced"}) == "cancelled"


def test_draft_is_offered_as_a_return_status():
    """A value `map_read` can return must be in the options the schema advertises,
    or the workspace filter cannot express what the list now shows."""
    from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import _STATUS_OPTIONS

    assert "draft" in [o["value"] for o in _STATUS_OPTIONS]
