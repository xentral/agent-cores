"""Shared upstream helpers for the stock entities (stockLevel, stockMovement).

Both adapters have to reach past their own ``v3_path`` into the scattered v1/v2
warehouse endpoints, and both need the same two primitives: a plain GET that
returns ``(status, json)`` without the facade's filter/sort translation, and the
``loc_…`` → ``(warehouseId, storageLocationId)`` resolution the item endpoints
need (they are keyed by warehouse AND location, our model only carries the
location).
"""

from __future__ import annotations

from typing import Any

import httpx

TIMEOUT = 60.0


def numeric(speaking_id: str) -> str:
    """``loc_3`` → ``3``; a bare numeric passes through unchanged."""
    return speaking_id.split("_", 1)[1] if "_" in speaking_id else speaking_id


async def get_json(
    url: str,
    params: list[tuple[str, str]],
    headers: dict[str, str],
    client: httpx.AsyncClient | None,
) -> tuple[int, Any]:
    async def _do(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get(url, params=params, headers=headers)

    if client is None:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            resp = await _do(c)
    else:
        resp = await _do(client)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


async def resolve_location_row(
    loc_id: str,
    *,
    base_url: str,
    headers: dict[str, str],
    client: httpx.AsyncClient | None,
) -> dict[str, Any] | None:
    """The raw v1 storageLocation row (id, designation, warehouse) for ``loc_…``.

    v1 storageLocations has no show route — the id filter on the list is the
    lookup."""
    num = numeric(loc_id)
    status, body = await get_json(
        f"{base_url.rstrip('/')}/api/v1/storageLocations",
        [
            ("page[number]", "1"),
            ("page[size]", "10"),
            ("filter[0][key]", "id"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", num),
        ],
        headers,
        client,
    )
    if status >= 400 or not isinstance(body, dict):
        return None
    rows = body.get("data") or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


async def resolve_location_pair(
    loc_id: str,
    *,
    base_url: str,
    headers: dict[str, str],
    client: httpx.AsyncClient | None,
) -> tuple[str, str] | None:
    """``loc_…`` → (warehouseId, storageLocationId) numerics — the key pair the
    v1/v2 item endpoints are addressed by."""
    row = await resolve_location_row(loc_id, base_url=base_url, headers=headers, client=client)
    if row is None:
        return None
    wh = row.get("warehouse")
    wh_id = wh.get("id") if isinstance(wh, dict) else wh
    return (str(wh_id), numeric(loc_id)) if wh_id else None


def parse_filters(query: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """``filter[0][key|op|value]`` triples → ``{model key: (op, value)}``.

    The adapters that bypass ``_get`` lose its reference-prefix stripping, so the
    values stay exactly as the caller sent them (``prd_61985``); callers strip
    with :func:`numeric` where an upstream id is needed.
    """
    by_index: dict[str, dict[str, str]] = {}
    for k, v in query:
        if not k.startswith("filter[") or "]" not in k:
            continue
        idx = k[len("filter[") : k.index("]")]
        facet = k.rsplit("[", 1)[-1].rstrip("]")
        if facet in ("key", "op", "value"):
            by_index.setdefault(idx, {})[facet] = v
    out: dict[str, tuple[str, str]] = {}
    for spec in by_index.values():
        key = spec.get("key")
        if key:
            out[key] = (spec.get("op") or "equals", spec.get("value") or "")
    return out
