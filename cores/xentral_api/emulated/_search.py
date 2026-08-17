"""Shared `search` filter support for emulated entity adapters.

The v3/v1 endpoints behind Product, Customer, Supplier and StorageLocation have
no native cross-field ``search`` filter key (only per-field filters). To give
every entity the same "type a string, match the fields a clerk would search"
behaviour, this module emulates ``search`` for those adapters as a server-side
OR fan-out: one filtered request per configured search field, results merged and
de-duplicated by id.

Per-field ``contains`` filtering is already honoured server-side by all these
endpoints (verified live), so the fan-out reuses that rather than pulling rows
and filtering in Python — search still works beyond the first page.

An adapter opts in by declaring a ``search_fields`` tuple and calling
``fan_out_search`` from its ``request`` when :func:`extract_search` returns a
value. Adapters whose upstream endpoint has a native ``search`` key just let it
pass through unchanged.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse

_TIMEOUT_SECONDS = 30.0
_DEFAULT_PAGE_SIZE = 25
# Substring match is what a clerk expects from a search box; only fall back to
# it, never override an explicit operator a caller sent.
_DEFAULT_OP = "contains"


def filter_groups(query: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    """Group ``filter[i][key|op|value]`` triples by their ``filter[i]`` prefix.

    Public because it is the one place that knows v3's filter shape, and callers
    outside this module need the same reading of it — the Neo facade asks whether
    the caller already filtered on a key before injecting one of its own.
    """
    groups: dict[str, dict[str, str]] = {}
    for key, value in query:
        if key.startswith("filter[") and "][" in key:
            prefix, suffix = key.rsplit("[", 1)
            groups.setdefault(prefix, {})[suffix.rstrip("]")] = value
    return groups


def extract_search(query: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Return ``(value, op)`` for a ``search`` filter in ``query``, else ``None``.

    ``op`` defaults to ``contains`` when the caller didn't specify one.
    """
    for parts in filter_groups(query).values():
        if parts.get("key") == "search":
            return parts.get("value", "") or "", parts.get("op") or _DEFAULT_OP
    return None


def strip_search(query: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``query`` without the ``search`` filter group; other filters stay.

    For an endpoint that searches natively the term travels on as the upstream's
    own top-level ``?search=`` parameter, so the group that carried it has to go.
    Left in place it would arrive as a filter on a key called ``search``, and
    several v3 list endpoints ignore an unknown filter while answering 200 with
    the whole collection — the caller would read that as a search result.
    """
    drop = {p for p, parts in filter_groups(query).items() if parts.get("key") == "search"}
    return [
        (k, v)
        for k, v in query
        if not (k.startswith("filter[") and "][" in k and k.rsplit("[", 1)[0] in drop)
    ]


def get_page_size(query: list[tuple[str, str]]) -> int:
    for key, value in query:
        if key == "page[size]":
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return _DEFAULT_PAGE_SIZE
    return _DEFAULT_PAGE_SIZE


def build_field_query(
    query: list[tuple[str, str]], field: str, op: str, value: str
) -> list[tuple[str, str]]:
    """Rebuild ``query`` for a single search field.

    Drops the ``search`` filter, keeps any other (non-search) filters as
    additional AND constraints so a scoped search still works, pins
    ``page[number]`` to 1 (search returns the first page of matches), and
    appends a ``contains`` filter on ``field`` at a fresh, non-colliding index.
    """
    groups = filter_groups(query)
    search_prefixes = {p for p, parts in groups.items() if parts.get("key") == "search"}
    kept_prefixes = [p for p in groups if p not in search_prefixes]

    indices = [int(m.group(1)) for p in kept_prefixes if (m := re.search(r"filter\[(\d+)\]", p))]
    new_idx = (max(indices) + 1) if indices else 0

    rebuilt: list[tuple[str, str]] = []
    seen_page_number = False
    for key, val in query:
        if key.startswith("filter[") and "][" in key:
            if key.rsplit("[", 1)[0] in search_prefixes:
                continue
        if key == "page[number]":
            rebuilt.append((key, "1"))
            seen_page_number = True
            continue
        rebuilt.append((key, val))
    if not seen_page_number:
        rebuilt.append(("page[number]", "1"))

    rebuilt.append((f"filter[{new_idx}][key]", field))
    rebuilt.append((f"filter[{new_idx}][op]", op))
    rebuilt.append((f"filter[{new_idx}][value]", value))
    return rebuilt


async def fan_out_search(
    adapter: Any,
    *,
    query: list[tuple[str, str]],
    value: str,
    op: str,
    search_fields: tuple[str, ...],
    base_url: str,
    token: str,
    accept_language: str | None,
    client: httpx.AsyncClient | None,
) -> AdapterResponse:
    """Emulate a cross-field ``search`` as an OR over ``search_fields``.

    Fires one list request per field (concurrently, sharing one HTTP client),
    then merges the rows and de-duplicates by id, capped at the caller's page
    size. ``meta.total`` is the merged count — a lower bound, since it only
    reflects the first page fetched per field, which is the honest number for a
    "roughly search" basis.
    """
    page_size = get_page_size(query)

    async def _run(request_client: httpx.AsyncClient) -> AdapterResponse:
        async def fetch(field: str) -> AdapterResponse:
            return await adapter.request(
                method="GET",
                handle=None,
                query=build_field_query(query, field, op, value),
                body=None,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=request_client,
            )

        responses = await asyncio.gather(
            *(fetch(field) for field in search_fields), return_exceptions=True
        )

        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for resp in responses:
            if isinstance(resp, Exception) or resp.status_code >= 400:
                continue
            try:
                data = json.loads(resp.content or b"{}")
            except (ValueError, TypeError):
                continue
            rows = data.get("data") if isinstance(data, dict) else None
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or row.get("uuid") or "")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                merged.append(row)

        body = {
            "data": merged[:page_size],
            "meta": {"total": len(merged), "count": min(len(merged), page_size)},
        }
        return AdapterResponse(
            200,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            {"content-type": "application/json"},
        )

    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as owned_client:
            return await _run(owned_client)
    return await _run(client)
