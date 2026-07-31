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


def test_stock_movement_is_write_only():
    assert set(StockMovementAdapter.manifest.operations) == {"create"}


def test_batch_and_serial_declare_nothing():
    assert StockMovementAdapter.manifest.operations != ()
    assert BatchAdapter.manifest.operations == ()
    assert SerialNumberAdapter.manifest.operations == ()


def test_stock_level_keeps_its_reads():
    """The one entity in this family whose reads WERE composable must keep them."""
    assert set(StockLevelAdapter.manifest.operations) == {"list", "read"}


def test_dead_reads_are_refused_locally_not_upstream():
    for adapter in (StockMovementAdapter(), BatchAdapter(), SerialNumberAdapter()):
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


def test_stock_movement_description_names_the_read_back_and_the_preferred_surface():
    """create stays as the primitive, but must route a caller to the named
    warehouse actions (ADR-017) rather than being the obvious first choice."""
    desc = StockMovementAdapter.manifest.description
    assert "StockLevel" in desc  # where the effect is verified
    assert "inventoryCount" in desc  # the retry-safe write, now an action
    assert "PREFER" in desc


def test_writes_still_reach_the_orchestrator():
    """The gate must not swallow create — an invalid body proves it got through
    to the validator (422) rather than being refused as unsupported (405)."""
    resp = _call(StockMovementAdapter(), "POST")
    assert resp.status_code == 422
    assert "invalid booking" in json.loads(resp.content)["title"]
