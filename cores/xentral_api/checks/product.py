"""xentral_api · Product — v3 read-only verification checks.

Product is a read-only master-data entity (v3 ``GET /api/v3/products`` list +
show + 30 sub-resource includes), not a business document — so it gets its own
read-oriented checks rather than the document write-roundtrip set in
``_builders``. Every check here is read-only (no ``--writes`` needed): it proves
the entity lists, reads one complete record (with all includes), honours the v3
filter/sort keys, and returns its sub-resource collections. Results feed
``ctx.verified`` (the per-capability manifest the UI badges read from).

The gateway plumbing (``_auth`` / ``_gw`` / ``_resolve_id`` / ``_record`` /
``_mark_*``) is the core's entity-check harness — entity-agnostic — so it is
reused from ``_builders`` rather than duplicated.
"""

from __future__ import annotations

from tests.tool_suite.harness import Check, Ctx

from ._builders import _gw, _mark_field, _mark_group, _record, _resolve_id

CAT = "masterdata"
ENTITY = "Product"
_FIXTURE = "product_id"

# The 30 read-only include collections the v3 endpoint embeds — kept in sync with
# the adapter's ``_INCLUDE_COLLECTIONS`` so the coverage report is complete.
_INCLUDES = (
    "salesPrices",
    "purchasePrices",
    "commissions",
    "deliveryThresholds",
    "calculationItems",
    "texts",
    "media",
    "properties",
    "options",
    "freeFields",
    "categories",
    "variants",
    "externalReferences",
    "salesChannels",
    "tags",
    "parts",
    "usedIn",
    "rawMaterials",
    "workInstructions",
    "functionProtocols",
    "crossSelling",
    "certificates",
    "stock",
    "storageLocations",
    "reservations",
    "batches",
    "serialNumbers",
    "bestBeforeDates",
    "warehouseMinimums",
    "packagingUnits",
)

# (field, op, value) probes over the exact keys the v3 list endpoint filters on.
# Values are harmless — each probe only needs HTTP 200 (rows optional).
_FILTER_PROBES: tuple[tuple[str, str, str], ...] = (
    ("number", "contains", "0"),
    ("name", "contains", "a"),
    ("ean", "contains", "1"),
    ("isVariant", "equals", "false"),
    ("isMatrixProduct", "equals", "false"),
    ("updatedAt", "greaterThan", "2000-01-01"),
)
_SORT_FIELDS = ("number", "name", "updatedAt")


def _list_check() -> Check:
    async def fn(ctx: Ctx):
        status, parsed = await _gw(ctx, ENTITY, "GET", query=[("page[size]", "5")])
        if status != 200:
            return (False, f"list Product: HTTP {status}")
        rows = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            return (False, "list did not return a data array")
        _mark_group(ctx, ENTITY, "operations", "list", "pass")
        for row in rows:
            if isinstance(row, dict):
                for key in row:
                    _mark_field(ctx, ENTITY, key, "read", "pass")
        return (True, f"Product list ok, {len(rows)} rows")

    return Check(name="product.list", category=CAT, fn=fn, kind="read")


def _read_all_check() -> Check:
    async def fn(ctx: Ctx):
        rid = await _resolve_id(ctx, ENTITY, _FIXTURE)
        if not rid:
            return (None, "no Product record on tenant")
        # No include param → the adapter requests the full detail include set.
        status, parsed = await _gw(ctx, ENTITY, "GET", handle=rid)
        if status != 200:
            return (False, f"read Product {rid}: HTTP {status}")
        rec = _record(parsed)
        if not rec:
            return (False, f"read Product {rid}: empty record")
        _mark_group(ctx, ENTITY, "operations", "read", "pass")
        for key, value in rec.items():
            _mark_field(ctx, ENTITY, key, "read", "pass")
            if isinstance(value, dict):
                for sub in value:
                    _mark_field(ctx, ENTITY, f"{key}.{sub}", "read", "pass")
        populated = sum(1 for v in rec.values() if v not in (None, "", [], {}))
        with_rows = [
            f"{name}({len(rec[name])})"
            for name in _INCLUDES
            if isinstance(rec.get(name), list) and rec[name]
        ]
        return (
            True,
            f"Product {rid}: read ok, {populated}/{len(rec)} fields populated; "
            f"{len(with_rows)} includes with rows: {', '.join(with_rows)[:160]}",
        )

    return Check(name="product.read_all", category=CAT, fn=fn, kind="read")


def _filter_check() -> Check:
    async def fn(ctx: Ctx):
        ok: list[str] = []
        bad: list[str] = []
        for field, op, value in _FILTER_PROBES:
            status, _ = await _gw(
                ctx,
                ENTITY,
                "GET",
                query=[
                    ("filter[0][key]", field),
                    ("filter[0][op]", op),
                    ("filter[0][value]", value),
                    ("page[size]", "1"),
                ],
            )
            (ok if status == 200 else bad).append(f"{field}:{status}")
        if bad:
            return (False, f"Product filters: ok={ok} | issues={bad}")
        _mark_group(ctx, ENTITY, "operations", "filter", "pass")
        return (True, f"Product filters ok ({', '.join(ok)})")

    return Check(name="product.filter", category=CAT, fn=fn, kind="read")


def _sort_check() -> Check:
    async def fn(ctx: Ctx):
        ok: list[str] = []
        bad: list[str] = []
        for field in _SORT_FIELDS:
            for direction in ("", "-"):
                value = f"{direction}{field}"
                status, _ = await _gw(
                    ctx, ENTITY, "GET", query=[("sort", value), ("page[size]", "1")]
                )
                (ok if status == 200 else bad).append(f"{value}:{status}")
        if bad:
            return (False, f"Product sorts: ok={ok} | issues={bad}")
        _mark_group(ctx, ENTITY, "operations", "sort", "pass")
        return (True, f"Product sorts ok ({len(ok)} directions)")

    return Check(name="product.sort", category=CAT, fn=fn, kind="read")


CHECKS = [
    _list_check(),
    _read_all_check(),
    _filter_check(),
    _sort_check(),
]
