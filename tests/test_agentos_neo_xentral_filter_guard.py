"""An undeclared filter is refused here instead of being passed upstream.

Several v3 list endpoints IGNORE a filter they do not know and answer 200 with
the whole collection. Verified on mvp: filtering merchandiseGroups by a name
that exists nowhere returned all 15 rows (the page size was ignored too). The
caller then reads an unfiltered list as a filtered one — worse than an error,
because nothing about the answer looks wrong.

So the facade refuses a filter key it does not declare. The declared contract
becomes the boundary rather than a suggestion.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.settings import MerchandiseGroupAdapter


def _no_upstream(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"filter leaked upstream: {request.url}")


def _list(adapter, filters: list[tuple[str, str]], handler=_no_upstream):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.request(
                method="GET",
                handle=None,
                query=filters,
                body=None,
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


def _f(key: str, value: str = "x", index: int = 0) -> list[tuple[str, str]]:
    return [
        (f"filter[{index}][key]", key),
        (f"filter[{index}][op]", "equals"),
        (f"filter[{index}][value]", value),
    ]


def test_an_undeclared_filter_never_reaches_the_upstream():
    resp = _list(ProductAdapter(), _f("production.hasBillOfMaterials", "true"))
    assert resp.status_code == 422
    body = json.loads(resp.content)
    assert "production.hasBillOfMaterials" in body["detail"]
    assert "number" in body["filterable"]  # the answer says what IS possible


def test_an_entity_that_filters_on_nothing_refuses_every_filter():
    """MerchandiseGroup is one of the endpoints that silently ignores filters —
    which is exactly why nothing may be forwarded to it."""
    resp = _list(MerchandiseGroupAdapter(), _f("name", "Handelsware"))
    assert resp.status_code == 422
    assert json.loads(resp.content)["filterable"] == []


def test_a_declared_filter_still_goes_through():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [], "meta": {"total": 0}})

    resp = _list(ProductAdapter(), _f("number", "E2E-0730-01"), handler)
    assert resp.status_code == 200
    assert seen and "number" in seen[0]


def test_one_bad_key_among_good_ones_still_refuses():
    """Dropping the unknown key and running the rest would answer with a result
    set wider than the caller asked for — the failure this guard exists for."""
    query = _f("number", "X", 0) + _f("nonsense", "1", 1)
    resp = _list(ProductAdapter(), query)
    assert resp.status_code == 422
    assert "nonsense" in json.loads(resp.content)["detail"]


def test_the_search_key_is_not_treated_as_a_filter():
    """Consolidated search arrives as filter[n][key]=search and is handled by the
    fan-out, not by the upstream filter contract."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [], "meta": {"total": 0}})

    resp = _list(ProductAdapter(), _f("search", "Zelt"), handler)
    assert resp.status_code == 200
