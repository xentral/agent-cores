"""A filter must not return records in a state you did not ask for.

The companion to the schema/write-path sweep, for the other direction. Two model
values mapped onto ONE upstream value means filtering by the first returns the
second as well — silently, because the records come back reading as themselves.

That is exactly the DeliveryNote defect: `shipped` and `delivered` both mapped to
`completed`, so asking for shipped returned the delivered ones. Upstream had a
distinct `sent` all along; the read map knew it and the filter map did not.

Whether a collision is FIXABLE depends on the upstream vocabulary, which only a
live probe can settle — if upstream really has one value for two model states,
the imprecision is inherent and belongs in the field's description instead. So
this lists them rather than failing: the guarantee is that no new one appears.
"""

from __future__ import annotations

import collections

from xentral_entity_cores.agentos_neo_xentral import CORE

# Model values that share an upstream value, measured today. A filter on any one
# of them returns all of them.
#
# DeliveryNote and Return are fixed on `fix/status-filter-mapping` (#89), where
# upstream turned out to have distinct values. The other three are unexamined —
# each needs the same live check before it can be called a bug or a limit.
KNOWN_COLLISIONS = {
    ("Quote", "status", "completed"): ("accepted", "expired"),
    ("Quote", "status", "cancelled"): ("declined",),
    ("SalesOrder", "status", "completed"): ("closed", "fulfilled"),
    ("DeliveryNote", "status", "completed"): ("delivered", "shipped"),
    ("Return", "status", "completed"): ("checked", "settled"),
    ("PurchaseOrder", "status", "completed"): ("closed", "received"),
}


def _prop(fields: dict, path: str) -> dict | None:
    cur: dict | None = fields
    parts = path.split(".")
    for i, part in enumerate(parts):
        cur = (cur or {}).get(part)
        if cur is None:
            return None
        if i < len(parts) - 1:
            cur = cur.get("properties")
    return cur


def _collisions() -> dict[tuple[str, str, str], tuple[str, ...]]:
    found: dict[tuple[str, str, str], tuple[str, ...]] = {}
    for adapter in CORE.emulated_adapters():
        key = adapter.manifest.key
        for field, value_map in (adapter.filter_value_maps or {}).items():
            counts = collections.Counter(value_map.values())
            options = [
                o.get("value") if isinstance(o, dict) else o
                for o in ((_prop(adapter.fields(), field) or {}).get("options") or [])
            ]
            for upstream, count in counts.items():
                sharing = tuple(sorted(k for k, v in value_map.items() if v == upstream))
                # Two model values on one upstream value, OR one model value
                # pointing at a DIFFERENT model value — both mean a filter
                # answers with a state the caller did not ask for.
                if count > 1 or (upstream in options and sharing != (upstream,)):
                    found[(key, field, upstream)] = sharing
    return found


def test_no_new_filter_value_collision_appears():
    """The list is the point: a new entry means someone made a filter that
    answers with the wrong state, and nobody would notice from the response."""
    assert _collisions() == KNOWN_COLLISIONS


def test_every_mapped_key_is_a_declared_option():
    """A map key that is not a model value can never be sent by a caller reading
    the schema — the entry is dead, and hides that the value is unfilterable."""
    stray = []
    for adapter in CORE.emulated_adapters():
        for field, value_map in (adapter.filter_value_maps or {}).items():
            spec = _prop(adapter.fields(), field) or {}
            options = [
                o.get("value") if isinstance(o, dict) else o for o in (spec.get("options") or [])
            ]
            if not options:
                continue
            stray += [(adapter.manifest.key, field, k) for k in value_map if k not in options]
    assert not stray, f"filter map keys that are not declared options: {stray}"
