"""Tag-filtered Product lists must carry the same paging envelope as any list.

v3 products rejects a ``tags`` filter, so the facade emulates it by scanning
upstream pages and matching mapped tag titles (``_list_by_tags``). That path
used to return ``{data, extra}`` with no ``meta`` at all — so a caller asking
for 2 of 4 matches received 2 rows and no way to learn more existed. Observed
on the mvp tenant: 541 rows scanned, 4 matched, and the real count reachable
only through ``extra.emulatedFilter.matched``.
"""

from __future__ import annotations

import asyncio
import json

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter


def _adapter_over(rows: list[dict], *, page_size: int) -> ProductAdapter:
    """A ProductAdapter whose upstream serves ``rows`` in ``page_size`` chunks."""
    adapter = ProductAdapter()

    async def _fake_get(base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001, ANN202
        q = dict(query)
        page = int(q.get("page[number]", "1"))
        start = (page - 1) * page_size
        return 200, {"data": rows[start : start + page_size]}

    adapter._get = _fake_get  # type: ignore[method-assign]
    return adapter


def _call(adapter: ProductAdapter, *, page: int, size: int) -> dict:
    resp = asyncio.run(
        adapter._list_by_tags(["erledigt"], [], page, size, "https://x.test", "tok", None, None)
    )
    assert resp.status_code == 200
    return json.loads(resp.content)


def _tagged(n: int) -> list[dict]:
    return [{"id": i, "name": f"p{i}", "tags": [{"title": "Erledigt"}]} for i in range(1, n + 1)]


def test_complete_scan_reports_total_and_last_page():
    body = _call(_adapter_over(_tagged(4), page_size=50), page=1, size=2)
    assert len(body["data"]) == 2
    assert body["meta"]["page"] == 1
    assert body["meta"]["perPage"] == 2
    assert body["meta"]["total"] == 4
    assert body["meta"]["lastPage"] == 2
    # Both count conventions agree, as on the generic list path.
    assert body["extra"]["total"] == 4


def test_second_page_is_reachable_from_the_envelope():
    rows = _tagged(4)
    first = _call(_adapter_over(rows, page_size=50), page=1, size=2)
    second = _call(_adapter_over(rows, page_size=50), page=2, size=2)
    assert [r["id"] for r in first["data"]] == ["prd_1", "prd_2"]
    assert [r["id"] for r in second["data"]] == ["prd_3", "prd_4"]
    assert second["meta"]["page"] == 2
    assert second["meta"]["lastPage"] == 2


def test_no_matches_still_carries_paging():
    rows = [{"id": 1, "name": "p1", "tags": [{"title": "Anderes"}]}]
    body = _call(_adapter_over(rows, page_size=50), page=1, size=25)
    assert body["data"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["lastPage"] == 1


def test_truncated_scan_omits_total_rather_than_understating_it():
    """A capped scan counted what it reached, not what exists."""
    capped = ProductAdapter._TAG_SCAN_MAX_PAGES * ProductAdapter._TAG_SCAN_PAGE_SIZE
    adapter = _adapter_over(_tagged(capped + 500), page_size=ProductAdapter._TAG_SCAN_PAGE_SIZE)
    body = _call(adapter, page=1, size=25)
    assert body["extra"]["emulatedFilter"]["truncated"] is True
    assert "total" not in body["meta"]
    assert "lastPage" not in body["meta"]
    assert "total" not in body["extra"]
    # Paging still reported, and the scan count stays available.
    assert body["meta"]["page"] == 1
    assert body["meta"]["perPage"] == 25
    assert body["extra"]["emulatedFilter"]["matched"] == capped
