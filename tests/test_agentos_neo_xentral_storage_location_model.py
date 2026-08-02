"""StorageLocation modelled in ERP vocabulary, not Xentral's own.

Xentral describes a bin with five independent booleans — Nachschublager,
Verbrauchslager, Sperrlager, Fertigungszugriff, Kassenplatz — plus an ABC class,
a picking sequence (`sort`) and physical dimensions. The model this replaced
declared a single-valued ``kind`` (picking|bulk|inbound|returns|quarantine) of
which four values existed nowhere upstream, and a ``capacity.maxWeight`` that
existed nowhere either.

Two upstream traits make the mapping non-obvious, and both are pinned here:

* the flat tenant-wide list answers ``id``/``designation``/``warehouse`` and
  NOTHING else — every characterising field rides on the warehouse-scoped
  collection, so a single read has to go there;
* the production flag is read as ``productionAccess`` and written as
  ``allowsProductionAccess`` (measured on mvp 2026-08-02, not assumed).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.storage_location import (
    StorageLocationAdapter,
)

_THIN = {"id": "1", "designation": "NL-01", "warehouse": {"id": "9", "name": "Hauptlager"}}
_RICH = {
    "id": "1",
    "designation": "NL-01",
    "description": "Nachschubzone A",
    "sort": 12,
    "abcCategory": "A",
    "isReplenishmentLocation": True,
    "isConsumptionLocation": False,
    "isRestrictedLocation": True,
    "productionAccess": True,
    "isPosLocation": False,
    "dimensions": {"length": 100, "width": 60, "height": 40},
}


_OTHER_WH = {"id": "2", "designation": "SP-01", "warehouse": {"id": "4", "name": "Sperrlager"}}
_RICH_2 = {"id": "2", "designation": "SP-01", "isRestrictedLocation": True, "abcCategory": "C"}


class _Upstream:
    """Answers the flat list thin and the scoped list rich, as mvp does."""

    def __init__(self, *, scoped_fails: bool = False):
        self.scoped_fails = scoped_fails
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/api/v1/storageLocations":
            return httpx.Response(
                200, json={"data": [_THIN, _OTHER_WH], "extra": {"totalCount": 2}}
            )
        if path == "/api/v1/warehouses/9/storageLocations":
            if self.scoped_fails:
                return httpx.Response(500, json={"title": "down"})
            return httpx.Response(200, json={"data": [_RICH], "extra": {"totalCount": 1}})
        if path == "/api/v1/warehouses/4/storageLocations":
            return httpx.Response(200, json={"data": [_RICH_2], "extra": {"totalCount": 1}})
        if path.startswith("/api/v2/warehouses/") and path.endswith("/items"):
            return httpx.Response(200, json={"data": [], "extra": {"totalCount": 0}})
        raise AssertionError(f"unexpected call: {request.method} {path}")


def _read(up: _Upstream, handle: str = "loc_1") -> dict:
    a = StorageLocationAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="GET",
                handle=handle,
                query=[],
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    resp = asyncio.run(go())
    assert resp.status_code == 200
    return json.loads(resp.content)["data"]


def test_a_single_read_carries_the_fields_the_flat_list_omits():
    """The id-filtered flat list names the bin; it does not describe it."""
    up = _Upstream()
    d = _read(up)
    assert d["usage"] == {
        "replenishment": True,
        "consumption": False,
        "blocked": True,
        "production": True,
        "pointOfSale": False,
    }
    assert d["abcCategory"] == "A"
    assert d["pickingOrder"] == 12
    assert d["dimensions"] == {"length": 100, "width": 60, "height": 40}
    assert "/api/v1/warehouses/9/storageLocations" in up.calls


def test_a_blocked_bin_reports_blocked_status():
    assert _read(_Upstream())["status"] == "blocked"


def test_the_enrichment_never_costs_a_read():
    """If the scoped collection is unreachable the read still answers what it has."""
    up = _Upstream(scoped_fails=True)
    d = _read(up)
    assert d["id"] == "loc_1"
    assert d["name"] == "NL-01"
    assert d["usage"]["replenishment"] is None  # honestly unknown, not False


def test_the_warehouse_name_survives_the_enrichment():
    """The scoped row omits the warehouse entirely — merging must not drop it."""
    d = _read(_Upstream())
    assert d["warehouse"]["id"] == "wh_9"
    assert d["warehouse"]["name"] == "Hauptlager"


def test_usage_writes_the_asymmetric_production_key():
    wire, rejected = StorageLocationAdapter().map_write(
        {"usage": {"replenishment": True, "production": True, "blocked": False}}, creating=False
    )
    assert wire["isReplenishmentLocation"] is True
    assert wire["allowsProductionAccess"] is True  # NOT `productionAccess`
    assert wire["isRestrictedLocation"] is False
    assert "productionAccess" not in wire
    assert not rejected


def test_picking_order_travels_as_sort():
    wire, _ = StorageLocationAdapter().map_write({"pickingOrder": 12}, creating=False)
    assert wire == {"sort": 12}


def test_a_partial_usage_leaves_the_other_flags_alone():
    """An update that names one flag must not silently reset the other four."""
    wire, _ = StorageLocationAdapter().map_write({"usage": {"blocked": True}}, creating=False)
    assert wire == {"isRestrictedLocation": True}


def test_description_is_read_only():
    """Upstream returns it and rejects it on create AND update with a 400.

    Declaring it writable would turn every write that carries a description into
    a failed request, so the core refuses it here instead — visibly.
    """
    spec = StorageLocationAdapter().fields()["description"]
    assert not spec.get("creatable")
    assert not spec.get("updatable")
    wire, rejected = StorageLocationAdapter().map_write(
        {"name": "NL-01", "description": "Nachschubzone A"}, creating=True
    )
    assert "description" not in wire
    assert "description" in rejected


def test_the_invented_vocabulary_is_gone():
    fields = StorageLocationAdapter().fields()
    assert "kind" not in fields
    assert "capacity" not in fields


def _list(up: _Upstream) -> list[dict]:
    a = StorageLocationAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.request(
                method="GET",
                handle=None,
                query=[],
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    resp = asyncio.run(go())
    assert resp.status_code == 200
    return json.loads(resp.content)["data"]


def test_a_list_describes_its_bins_and_does_not_only_name_them():
    """Without this the answer to "which bins are blocked" is unanswerable from a
    list: the flat endpoint returns designation and warehouse, nothing else."""
    rows = {r["id"]: r for r in _list(_Upstream())}
    assert rows["loc_1"]["usage"]["replenishment"] is True
    assert rows["loc_1"]["abcCategory"] == "A"
    assert rows["loc_2"]["usage"]["blocked"] is True
    assert rows["loc_2"]["status"] == "blocked"
    assert rows["loc_2"]["abcCategory"] == "C"


def test_the_page_costs_one_call_per_warehouse_not_per_row():
    up = _Upstream()
    _list(up)
    scoped = [c for c in up.calls if c.startswith("/api/v1/warehouses/")]
    assert sorted(scoped) == [
        "/api/v1/warehouses/4/storageLocations",
        "/api/v1/warehouses/9/storageLocations",
    ]


def test_a_list_row_keeps_its_warehouse_name():
    rows = {r["id"]: r for r in _list(_Upstream())}
    assert rows["loc_1"]["warehouse"]["name"] == "Hauptlager"
    assert rows["loc_2"]["warehouse"]["name"] == "Sperrlager"


def test_an_unreachable_warehouse_does_not_break_the_page():
    """A scoped list that fails leaves those rows thin — the page still answers."""
    rows = {r["id"]: r for r in _list(_Upstream(scoped_fails=True))}
    assert rows["loc_1"]["name"] == "NL-01"  # warehouse 9 failed
    assert rows["loc_2"]["usage"]["blocked"] is True  # warehouse 4 did not
