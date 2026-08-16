"""The stock entities must not declare operations their upstream cannot serve.

Three entities advertised `list`/`read` against routes that do not exist, so every
caller learned the gap from a raw upstream 404 (`code 7431, Route not found`)
instead of from the contract:

  * StockMovement — no stock-ledger API at all (docs/05 #1)
  * Batch / SerialNumber — no batch or serial resource at all (docs/05 #4)

A declared-but-dead operation is worse than an absent one: an agent plans from
`describe`, so it plans a read that cannot work and cannot tell why it failed.
These pin the honest contract and that the gate refuses the dead ops locally,
without a round-trip to Xentral.

StockMovement has since gained its reads: `GET /api/v3/stockMovements` shipped
with API-805 and was verified live on mvp, so its `list`/`read` are declared and
the entity is no longer part of the dead-read set. The rule is unchanged — the
declaration follows the route, in both directions. What stays refused there is
`update`/`delete`: the ledger is append-only upstream.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.batch import BatchAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.serial_number import SerialNumberAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.stock_level import StockLevelAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.stock_movement import StockMovementAdapter


def _no_upstream(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"gate leaked to upstream: {request.method} {request.url.path}")


def _call(adapter, method: str, handle: str | None = None):
    async def go():
        transport = httpx.MockTransport(_no_upstream)
        async with httpx.AsyncClient(transport=transport) as client:
            return await adapter.request(
                method=method,
                handle=handle,
                query=[],
                body=b"{}" if method == "POST" else None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def test_stock_movement_reads_the_ledger_but_never_mutates_it():
    assert set(StockMovementAdapter.manifest.operations) == {"list", "read", "create"}


def test_stock_movement_refuses_update_and_delete_locally():
    """The ledger is append-only upstream; neither op has a route to reach."""
    for method in ("PATCH", "DELETE"):
        resp = _call(StockMovementAdapter(), method, "stm_1")
        assert resp.status_code == 405, method
        assert "not supported" in json.loads(resp.content)["title"]


def test_batch_and_serial_declare_nothing():
    assert StockMovementAdapter.manifest.operations != ()
    assert BatchAdapter.manifest.operations == ()
    assert SerialNumberAdapter.manifest.operations == ()


def test_stock_level_keeps_its_reads():
    """The one entity in this family whose reads WERE composable must keep them."""
    assert set(StockLevelAdapter.manifest.operations) == {"list", "read"}


def test_dead_reads_are_refused_locally_not_upstream():
    for adapter in (BatchAdapter(), SerialNumberAdapter()):
        for method, handle in (("GET", None), ("GET", "x_1")):
            resp = _call(adapter, method, handle)
            assert resp.status_code == 405, (adapter.manifest.key, handle)
            body = json.loads(resp.content)
            assert "not supported" in body["title"]


def test_every_dead_entity_says_why_in_its_catalogue_description():
    """The 405 states WHAT is unsupported; `description` carries the WHY into
    `list`, which is where an agent picks an entity in the first place."""
    for adapter in (StockMovementAdapter, BatchAdapter, SerialNumberAdapter):
        desc = adapter.manifest.description
        assert desc, adapter.manifest.key
        assert len(desc) > 40


def test_stock_movement_description_still_routes_writes_to_the_named_actions():
    """Gaining reads must not turn the low-level booking primitive into the
    obvious way to WRITE stock — the named warehouse actions stay preferred
    (ADR-017), and the differing grain has to be stated where an agent picks the
    entity."""
    desc = StockMovementAdapter.manifest.description
    assert "PREFER" in desc
    assert "inventoryCount" in desc  # the retry-safe write, an action
    assert "grain" in desc  # one create, two ledger rows


def test_writes_still_reach_the_orchestrator():
    """The gate must not swallow create — an invalid body proves it got through
    to the validator (422) rather than being refused as unsupported (405)."""
    resp = _call(StockMovementAdapter(), "POST")
    assert resp.status_code == 422
    assert "invalid booking" in json.loads(resp.content)["title"]
