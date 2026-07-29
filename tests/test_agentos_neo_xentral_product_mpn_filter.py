"""Product filter by manufacturer number (MPN) — emulated via v2 products.

v3 products has no manufacturerNumber filter (it 400s: "filter not allowed"), but
a supplier price list keys rows by the manufacturer number, so an agent must be
able to resolve product-by-MPN. The facade routes a standalone
``identifiers.manufacturerNumber`` filter to ``GET /api/v2/products`` (which
supports it) to get the ids, then reads them back through the normal v3 path so
the rows are in the model shape.
"""

from __future__ import annotations

import asyncio
from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter

_MPN = "identifiers.manufacturerNumber"


def _q(*triplets: tuple[str, str, str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for i, (key, op, value) in enumerate(triplets):
        out += [
            (f"filter[{i}][key]", key),
            (f"filter[{i}][op]", op),
            (f"filter[{i}][value]", value),
        ]
    return out


# ---- _split_mpn_filter (pure) -------------------------------------------
def test_split_detects_standalone_mpn():
    values, page, size = ProductAdapter._split_mpn_filter(
        _q((_MPN, "equals", "MPN-ACME-9981")) + [("page[number]", "2"), ("page[size]", "10")]
    )
    assert values == ["MPN-ACME-9981"]
    assert page == 2 and size == 10


def test_split_ignores_when_no_mpn():
    values, _, _ = ProductAdapter._split_mpn_filter(_q(("number", "equals", "1000486")))
    assert values is None


def test_split_falls_through_when_combined_with_other_filter():
    # MPN + another filter → None, so it falls through to v3 (honest "not allowed").
    values, _, _ = ProductAdapter._split_mpn_filter(
        _q((_MPN, "equals", "MPN-ACME-9981"), ("status", "equals", "active"))
    )
    assert values is None


# ---- integration: v2 resolve → v3 read ----------------------------------
class _Resp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Serves GET /api/v2/products (the MPN resolver); records the params seen."""

    def __init__(self, matches: list[dict]) -> None:
        self.matches = matches
        self.v2_params: list[Any] = []

    async def get(self, url: str, params: Any = None, headers: dict | None = None) -> _Resp:
        assert url.endswith("/api/v2/products")
        self.v2_params.append(params)
        return _Resp(200, {"data": self.matches})


def _adapter_with_v3(product_by_id: dict[str, dict]) -> ProductAdapter:
    adapter = ProductAdapter()

    async def _fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        rec = product_by_id.get(str(handle))
        return (200, {"data": rec}) if rec else (404, {"title": "nope"})

    adapter._get = _fake_get  # type: ignore[method-assign]
    return adapter


def _run(adapter: ProductAdapter, client: _FakeClient, query: list[tuple[str, str]]):
    return asyncio.run(
        adapter.request(
            method="GET",
            handle=None,
            query=query,
            body=None,
            base_url="https://x.test",
            token="tok",
            accept_language=None,
            client=client,
        )
    )


def test_mpn_filter_resolves_via_v2_then_reads_v3():
    import json

    client = _FakeClient(matches=[{"id": "61976"}])
    adapter = _adapter_with_v3({"61976": {"id": "61976", "name": "SupTest 01"}})
    resp = _run(adapter, client, _q((_MPN, "equals", "MPN-ACME-9981")))
    assert resp.status_code == 200
    body = json.loads(resp.content)
    # exactly the resolved product, in model shape (speaking id)
    assert [r["id"] for r in body["data"]] == ["prd_61976"]
    assert body["data"][0]["name"] == "SupTest 01"
    assert body["extra"]["emulatedFilter"]["field"] == _MPN
    assert body["extra"]["total"] == 1
    # v2 was queried with the manufacturerNumber filter
    flat = dict(client.v2_params[0])
    assert flat["filter[0][key]"] == "manufacturerNumber"
    assert flat["filter[0][value]"] == "MPN-ACME-9981"


def test_mpn_filter_no_match_returns_empty_page():
    import json

    client = _FakeClient(matches=[])
    adapter = _adapter_with_v3({})
    resp = _run(adapter, client, _q((_MPN, "equals", "NOPE-000")))
    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert body["data"] == []
    assert body["extra"]["total"] == 0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
