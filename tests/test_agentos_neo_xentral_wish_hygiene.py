"""A wish must not outlive the gap it describes, and must not hide when it has no field.

A blue wish outranks every other verdict in every capability view — including a
`pass`. That is right for a real gap and dangerous for a stale one: the entry goes
on claiming "not possible" over a capability that has since been built, and it is
the *proof* that gets hidden. Six such entries were sitting in this core, among
them `references.customerOrderNumber` on four document types, whose update a live
probe had already shown working.

Declaration is the wrong yardstick for retiring one. `Product.suppliers` declares
a writable child — the default supplier maps to v2 `standardSupplier` — while
multi-supplier sourcing, which is what the wish is about, has no write path at
all; retiring it on the flag would erase a real gap. A recorded `pass` cannot be
argued with: something wrote the value and read it back.

`read` is excluded from that evidence. verify.py stamps `read: pass` on every
declared path whether or not the instance carried a value, so it says "the schema
has this field", not "upstream supplies it" — which is exactly what a read wish
disputes. Taking it as proof retired 50 legitimate wishes in one run.

The other half: a wish for a field the schema does not have (`items.totals.tax`,
`contacts.address`) had no row to colour and vanished from every view. Those are
the widest gaps in the backlog, so they are now named explicitly.
"""

from __future__ import annotations

from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.manifest import CORE


class _Adapter(QuoteAdapter):
    """Quote with the two data sources stubbed, so the rule can be driven without
    editing the committed backlog."""

    def __init__(self, wishes: dict[str, dict[str, str]], verified: dict[str, Any]) -> None:
        super().__init__()
        self._wishes = wishes
        self._verified = verified

    def _wishes_by_field(self) -> dict[str, dict[str, str]]:
        return self._wishes

    def _proven(self, field: str, op: str) -> bool:
        from xentral_entity_cores.agentos_neo_xentral.emulated.base import _EARNED_VERDICTS

        if op not in _EARNED_VERDICTS:
            return False
        if (self._verified.get(field) or {}).get(op) == "pass":
            return True
        return any(
            p.startswith(f"{field}.") and (f or {}).get(op) == "pass"
            for p, f in self._verified.items()
        )


def _priority(adapter: QuoteAdapter, path: str) -> dict[str, str] | None:
    props = adapter.fields()
    adapter._apply_priorities(props)
    spec = adapter._resolve_path(props, path) if "." in path else props.get(path)
    return (spec or {}).get("priority")


WISH = {"texts.intro": {"update": "not writable upstream"}}


def test_an_unproven_wish_still_renders() -> None:
    assert _priority(_Adapter(WISH, {}), "texts.intro") == {"update": "not writable upstream"}


def test_a_proven_op_is_not_stamped() -> None:
    """Otherwise the blue cell buries the green one underneath it."""
    a = _Adapter(WISH, {"texts.intro": {"update": "pass"}})
    assert _priority(a, "texts.intro") is None


def test_a_failed_probe_leaves_the_wish_alone() -> None:
    """`fail` is not proof that it works — quite the opposite."""
    a = _Adapter(WISH, {"texts.intro": {"update": "fail"}})
    assert _priority(a, "texts.intro") == {"update": "not writable upstream"}


def test_only_the_proven_op_is_dropped() -> None:
    a = _Adapter(
        {"texts.intro": {"create": "nope", "update": "nope"}},
        {"texts.intro": {"update": "pass"}},
    )
    assert _priority(a, "texts.intro") == {"create": "nope"}


def test_a_read_pass_is_no_proof() -> None:
    """verify.py marks every declared path read-pass by construction."""
    a = _Adapter(
        {"texts.intro": {"read": "upstream never fills this"}}, {"texts.intro": {"read": "pass"}}
    )
    assert _priority(a, "texts.intro") == {"read": "upstream never fills this"}


def test_a_container_counts_a_proven_leaf() -> None:
    """`items` is editable exactly when a line field has been shown to update."""
    a = _Adapter(
        {"items": {"update": "no line-item write path"}}, {"items.description": {"update": "pass"}}
    )
    assert _priority(a, "items") is None


def test_the_reporter_names_what_the_stamp_skipped() -> None:
    """Dropping it from the view silently would be the same failure in reverse."""
    a = _Adapter(WISH, {"texts.intro": {"update": "pass"}})
    assert a.obsolete_wishes() == [{"field": "texts.intro", "ops": ["update"]}]


def test_the_committed_backlog_carries_no_obsolete_entry() -> None:
    """This is the one that goes red when a capability is built and the backlog is
    not updated with it."""
    stale = {a.manifest.key: a.obsolete_wishes() for a in CORE.adapters if a.obsolete_wishes()}
    assert stale == {}, stale


# ---- wishes with no field to colour --------------------------------------


def test_a_wish_for_a_missing_field_is_named() -> None:
    a = _Adapter({"items.totals.tax": {"read": "no per-line tax amount upstream"}}, {})
    assert a.missing_field_wishes() == [
        {"field": "items.totals.tax", "ops": ["read"], "reason": "no per-line tax amount upstream"}
    ]


def test_a_wish_with_a_field_is_not_listed_there() -> None:
    assert _Adapter(WISH, {}).missing_field_wishes() == []


@pytest.mark.parametrize("key", ["Quote", "SalesOrder", "SalesInvoice", "CreditNote"])
def test_the_per_line_tax_gap_is_visible_on_every_sales_document(key: str) -> None:
    """The one that used to vanish: v3 states only taxRate/effectiveTaxRate on a
    line, so the tax a position carries cannot be read — and the core will not
    derive it from gross minus net."""
    adapter = next(a for a in CORE.adapters if a.manifest.key == key)
    assert any(w["field"] == "items.totals.tax" for w in adapter.missing_field_wishes())


def test_describe_carries_them() -> None:
    meta = next(a for a in CORE.adapters if a.manifest.key == "Quote").metadata()
    assert any(w["field"] == "items.totals.tax" for w in meta["missingFieldWishes"])


def test_an_entity_with_none_says_nothing() -> None:
    assert "missingFieldWishes" not in ProductAdapter().metadata()
