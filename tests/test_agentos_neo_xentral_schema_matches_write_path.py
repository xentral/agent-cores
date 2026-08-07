"""Does every entity's schema agree with its own write path?

Almost every defect found while rebuilding the workflow library had one shape: a
declaration says A and the code beside it does B. `prices.purchase.source` was
marked read-only and was honoured by `map_write`. `dunning` was advertised as
writable and rejected outright. `Return.status` was filterable against a field it
is not read from. Each was found by hand, days apart, by someone tripping over it.

This asks all fifty at once, and it is deliberately narrow: for every field the
schema declares writable, `map_write` must accept it AND put something in the
upstream body — in the mode that declaration applies to.

Two things the probe learned the hard way, both encoded here:

* "not rejected" is NOT "written". `id`, `object` and `createdAt` sit in the
  adapters' `_IGNORE` list and are dropped on purpose so a caller may echo a
  record back. Comparing the body against an empty write is what separates them.
* A `creatable`-only field is SUPPOSED to be refused on an update. Testing every
  flag in one mode reported fifteen healthy fields as broken.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral import CORE

# Deviations as measured today. Every entry is a field whose declaration and
# write path disagree — either refused, or accepted and silently dropped.
#
# They are listed, not silenced: the point of the sweep is that no NEW one can
# appear unnoticed. Each still needs confirming per entity, because an adapter
# may compose that field outside `map_write` (salesOrder splits its line items
# off before delegating, and a probe cannot see that from here).
KNOWN_DEVIATIONS = {
    #
    # StockMovement has it exactly backwards, and the endpoint that is arriving
    # proves it. xentral/xentral#24580 adds GET /api/v3/stockMovements — the
    # warehouse ledger (`lager_bewegung`), READ-ONLY, scope `stockMovement:read`,
    # no POST. So:
    #
    #   * `create` is declared and will NEVER work there — these eight fields
    #     have nothing to reach, now or after that PR ships.
    #   * `list`/`read` are deliberately NOT declared, on the reasoning that
    #     "there is no stock-ledger API upstream (verified 404 on mvp)" — which is
    #     precisely what #24580 provides.
    #
    # Left as-is until that PR lands: declaring list/read today would 404. When it
    # does land, swap the operations (drop `create`, declare `list`/`read` with
    # the filters it exposes: product.id, storageLocation.id, warehouse.id,
    # direction, postedAt, causedBy.*) and these eight entries go with it.
    ("StockMovement", "type", "create"),
    ("StockMovement", "product", "create"),
    ("StockMovement", "quantity", "create"),
    ("StockMovement", "from", "create"),
    ("StockMovement", "to", "create"),
    ("StockMovement", "setQuantityTo", "create"),
    ("StockMovement", "batch", "create"),
    ("StockMovement", "source", "create"),
    # `Product.status` and `Product.project` are declared creatable and are NOT
    # in the v2 create body — measured: an update emits `isDisabled` / `project`,
    # a create emits nothing. Fixed in the same change that added this note, so
    # these two are gone; kept here only if the flags are ever restored.
}


def _sample(spec: dict):
    """A plausible value for a field, derived from its own declared type."""
    kind = spec.get("type")
    if kind == "select":
        options = spec.get("options") or []
        first = options[0] if options else None
        return (first.get("value") if isinstance(first, dict) else first) or "x"
    if kind == "boolean":
        return True
    if kind in ("integer", "decimal"):
        # NOT 1: several adapters default a quantity to 1, so the sample would
        # equal the baseline body and the field would read as silently dropped.
        # That is exactly how PriceList/PurchasePrice `minQuantity` landed on the
        # deviation list — a flaw in the probe, not in the core.
        return 7
    if kind == "date":
        return "2026-01-01"
    if kind == "datetime":
        return "2026-01-01T00:00:00+00:00"
    if kind == "reference":
        return {"id": "ref_1"}
    if kind == "tag":
        return ["x"]
    if kind == "embedded":
        nested = {
            k: _sample(v) for k, v in (spec.get("properties") or {}).items() if isinstance(v, dict)
        }
        return nested or {"x": "y"}
    if kind == "collection":
        node = (spec.get("node") or {}).get("properties") or {}
        return [{k: _sample(v) for k, v in node.items() if isinstance(v, dict)}]
    return "x"


# `map_write` is not the only write path. `PartnerSubresourcesMixin.request`
# splits `contacts` and `addresses` off BEFORE the base write and syncs them
# through their own endpoints, so probing `map_write` alone sees a rejection that
# never happens. Skipped explicitly rather than listed as deviations — the sweep
# has to be honest about what it cannot see, or it invites exactly the wrong fix
# (these were briefly marked read-only on the strength of that false positive).
_WRITTEN_ABOVE_MAP_WRITE = {"contacts", "addresses"}


def _uses_subresource_mixin(adapter) -> bool:  # noqa: ANN001
    return any(base.__name__ == "PartnerSubresourcesMixin" for base in type(adapter).__mro__)


def _cases():
    for adapter in CORE.emulated_adapters():
        ops = adapter.manifest.operations
        for name, spec in (adapter.fields() or {}).items():
            if not isinstance(spec, dict) or spec.get("access") == "readOnly":
                continue
            if name in _WRITTEN_ABOVE_MAP_WRITE and _uses_subresource_mixin(adapter):
                continue
            for flag, creating, op in (
                ("creatable", True, "create"),
                ("updatable", False, "update"),
            ):
                if spec.get(flag) and op in ops:
                    yield adapter.manifest.key, adapter, name, spec, creating, op


@pytest.mark.parametrize(
    ("key", "adapter", "name", "spec", "creating", "op"),
    [pytest.param(*c, id=f"{c[0]}.{c[2]}.{c[5]}") for c in _cases()],
)
def test_a_field_the_schema_calls_writable_reaches_the_upstream_body(
    key, adapter, name, spec, creating, op
):
    """Declared writable must mean written — in the mode that declaration is for.

    A field that is refused, or accepted and then dropped, is a schema that lies
    to every reader: an agent planning a write believes it can set the field, and
    the run reports success while the value never leaves the building.
    """
    if (key, name, op) in KNOWN_DEVIATIONS:
        pytest.skip("known deviation — see KNOWN_DEVIATIONS")
    baseline, _ = adapter.map_write({}, creating=creating)
    body, rejected = adapter.map_write({name: _sample(spec)}, creating=creating)
    assert name not in (rejected or set()), f"{key}.{name} is declared {op}-able but refused"
    assert body != baseline, f"{key}.{name} is declared {op}-able but never reaches the body"


def test_the_deviation_list_has_no_stale_entries():
    """An entry that no longer deviates must be deleted, or the list turns into
    a place where a fixed field is quietly still excused."""
    stale = []
    for key, adapter, name, spec, creating, op in _cases():
        if (key, name, op) not in KNOWN_DEVIATIONS:
            continue
        baseline, _ = adapter.map_write({}, creating=creating)
        body, rejected = adapter.map_write({name: _sample(spec)}, creating=creating)
        if name not in (rejected or set()) and body != baseline:
            stale.append((key, name, op))
    assert not stale, f"no longer deviating — remove from KNOWN_DEVIATIONS: {stale}"
