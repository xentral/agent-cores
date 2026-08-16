"""StockMovement reads the warehouse ledger (API-805).

The entity was write-only because the upstream had no ledger read. It has one
now, and these pin the parts of that mapping that a passing HTTP call would not
prove — the ones measured against mvp on 2026-08-16 rather than read off a spec:

  * the model's field names reach the upstream's (``bookedAt`` → ``postedAt``,
    ``product`` → ``product.id``, ``source.reason`` → ``reference``),
  * a datetime filter is NOT trimmed to a date — the collection rejects a bare
    date, and filtering the column itself is what allows an index on ``zeit``,
  * the single read goes through ``filter[id]`` because ``/{id}`` is a 404,
  * the write grain is not the read grain: ``type``/``from``/``to`` stay null on
    a posting, and nothing derives a type from a direction,
  * a cause outside the eight modelled entities reports type and id but no
    record, rather than a speaking id pointing nowhere,
  * nullable references survive: the legacy placeholder reports no product /
    location / project instead of ``prd_None``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.stock_movement import StockMovementAdapter

# One row as mvp returns it (ids and shape taken from the live probe).
_ROW: dict[str, Any] = {
    "id": "74284465",
    "product": {"id": "62006"},
    "storageLocation": {"id": "163"},
    "quantity": 5,
    "direction": "inbound",
    "stockLevelAfter": 5,
    "postedAt": "2026-08-07T15:10:04+02:00",
    "reference": "Goods receipt from purchase order 100053",
    "editor": "Benedikt Sauter",
    "project": None,
    "causedBy": {"type": "purchaseOrder", "id": "185"},
    "stockMovementType": None,
    "systemType": None,
    "stockTransactionId": None,
    "createdAt": "2026-08-07T15:10:04+02:00",
    "updatedAt": "2026-08-07T15:10:04+02:00",
}


class _Upstream:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = [_ROW] if rows is None else rows
        self.calls: list[httpx.URL] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url)
        if request.url.path != "/api/v3/stockMovements":
            # A single read must never build /{id} — that route is a 404 upstream.
            return httpx.Response(404, json={"title": "Route not found"})
        return httpx.Response(
            200, json={"data": self.rows, "meta": {"currentPage": 1, "perPage": 25}}
        )

    def params(self) -> dict[str, str]:
        return dict(self.calls[-1].params)


def _call(up: _Upstream, *, handle: str | None = None, query: list[tuple[str, str]] | None = None):
    adapter = StockMovementAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await adapter.request(
                method="GET",
                handle=handle,
                query=query or [],
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def _one(up: _Upstream, **kw) -> dict[str, Any]:
    resp = _call(up, **kw)
    assert resp.status_code == 200, resp.content
    body = json.loads(resp.content)
    data = body["data"]
    return data[0] if isinstance(data, list) else data


# ---- mapping ---------------------------------------------------------------


def test_a_posting_maps_onto_the_model():
    row = _one(_Upstream())
    assert row["object"] == "stockMovement"
    assert row["id"] == "stm_74284465"
    assert row["direction"] == "inbound"
    assert row["product"]["id"] == "prd_62006"
    assert row["storageLocation"]["id"] == "loc_163"
    assert row["quantity"] == {"value": 5, "unit": None}
    assert row["stockLevelAfter"] == 5
    assert row["bookedAt"] == "2026-08-07T15:10:04+02:00"
    assert row["source"] == {
        "reason": "Goods receipt from purchase order 100053",
        "editor": "Benedikt Sauter",
    }


def test_the_write_vocabulary_stays_empty_on_a_posting():
    """A ledger row is one side of a booking, so no `type` can be honest here: a
    transfer is two rows and an inbound one could equally be a receipt or a
    positive correction."""
    row = _one(_Upstream())
    assert row["type"] is None
    assert row["from"] is None and row["to"] is None
    assert row["setQuantityTo"] is None


def test_the_warehouse_is_filterable_but_never_invented():
    """It filters through the storage location and is not echoed upstream —
    reporting one would mean guessing."""
    row = _one(_Upstream())
    assert row["warehouse"] is None
    assert "warehouse" in StockMovementAdapter()._filterable_keys()


def test_a_modelled_cause_carries_a_record_to_read():
    row = _one(_Upstream())
    assert row["causedBy"]["type"] == "purchaseOrder"
    assert row["causedBy"]["id"] == "185"
    assert row["causedBy"]["record"]["id"] == "po_185"


def test_an_unmodelled_cause_reports_type_and_id_but_no_record():
    """`parcelReceipt` has no entity in this core. A speaking id pointing at one
    that does not exist would be worse than none."""
    up = _Upstream([{**_ROW, "causedBy": {"type": "parcelReceipt", "id": "77"}}])
    caused = _one(up)["causedBy"]
    assert caused["type"] == "parcelReceipt"
    assert caused["id"] == "77"
    assert caused["record"] is None


def test_a_posting_without_a_cause_reports_none():
    up = _Upstream([{**_ROW, "causedBy": None}])
    assert _one(up)["causedBy"] is None


def test_nullable_references_survive_the_legacy_placeholder():
    """product / storageLocation / project are all nullable upstream — the plain
    integer columns carry `0` for "none" and the write paths do not rule it out."""
    up = _Upstream([{**_ROW, "product": None, "storageLocation": None, "project": None}])
    row = _one(up)
    assert row["product"] is None
    assert row["storageLocation"] is None
    assert row["project"] is None


def test_an_outbound_quantity_stays_negative():
    """Signed upstream so a period can be summed without evaluating direction —
    the facade must not normalise the sign away."""
    up = _Upstream([{**_ROW, "quantity": -5, "direction": "outbound"}])
    row = _one(up)
    assert row["quantity"]["value"] == -5
    assert row["direction"] == "outbound"


def test_unit_cost_stays_empty_rather_than_absent():
    """Movement valuation is not exposed upstream at all (backlog blue wish); the
    shape is kept so a consumer does not have to branch on a missing key."""
    assert _one(_Upstream())["unitCost"] == {"amount": None, "currency": None}


# ---- query translation ------------------------------------------------------


def test_model_filter_keys_reach_the_upstream_names():
    up = _Upstream()
    _call(
        up,
        query=[
            ("filter[0][key]", "product"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "prd_62006"),
            ("filter[1][key]", "source.reason"),
            ("filter[1][op]", "contains"),
            ("filter[1][value]", "purchase"),
        ],
    )
    params = up.params()
    assert params["filter[0][key]"] == "product.id"
    assert params["filter[0][value]"] == "62006"  # speaking prefix stripped
    assert params["filter[1][key]"] == "reference"


def test_a_datetime_filter_keeps_its_time():
    """The collection answers 400 to a bare date, and comparing the column itself
    is what lets an index on `zeit` be used (SPS-147). Trimming — which the base
    does for the endpoints that need it — would break both."""
    up = _Upstream()
    _call(
        up,
        query=[
            ("filter[0][key]", "bookedAt"),
            ("filter[0][op]", "greaterThanOrEquals"),
            ("filter[0][value]", "2026-07-01T00:00:00+02:00"),
        ],
    )
    params = up.params()
    assert params["filter[0][key]"] == "postedAt"
    assert params["filter[0][value]"] == "2026-07-01T00:00:00+02:00"


def test_sorting_translates_and_keeps_a_deterministic_tiebreak():
    up = _Upstream()
    _call(up, query=[("sort", "-bookedAt")])
    assert up.params()["sort"] == "-postedAt,id"


def test_an_undeclared_filter_is_refused_before_the_upstream():
    """This collection ignores what it does not know, which reads as a filtered
    answer. The guard has to fire here, not there."""
    up = _Upstream()
    resp = _call(
        up,
        query=[
            ("filter[0][key]", "systemType"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "manual"),
        ],
    )
    assert resp.status_code == 422
    assert up.calls == []
    assert "systemType" in json.loads(resp.content)["detail"]


# ---- single read ------------------------------------------------------------


def test_read_one_goes_through_the_id_filter():
    """`GET /api/v3/stockMovements/{id}` is a 404 — the detail route was left out
    of scope, with filter[id] named as the substitute."""
    up = _Upstream()
    row = _one(up, handle="stm_74284465")
    assert row["id"] == "stm_74284465"
    params = up.params()
    assert params["filter[0][key]"] == "id"
    assert params["filter[0][value]"] == "74284465"
    assert up.calls[-1].path == "/api/v3/stockMovements"


def test_read_one_reports_404_when_the_filter_finds_nothing():
    up = _Upstream([])
    resp = _call(up, handle="stm_999")
    assert resp.status_code == 404


def test_read_one_refuses_a_malformed_id_without_asking_upstream():
    up = _Upstream()
    resp = _call(up, handle="stm_abc")
    assert resp.status_code == 422
    assert up.calls == []
