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

Which verdicts count as that proof used to be a hand-maintained allowlist of
facets, because the probe stamped `read: pass` on every declared path whether or
not the instance carried a value — taking that as proof retired 50 legitimate
wishes in one run. The weak claims now say so in the verdict itself, so the rule is
`is_proven` and nothing else: `accepted`, `unobserved`, `executed` and `reachable`
retire nothing, whichever facet they sit on.

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
from xentral_entity_cores.agentos_neo_xentral.verdicts import PROVEN, VERDICTS, is_proven


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
        """Mirrors the real rule against injected data — it must not restate it, or
        the test would keep passing after the rule changed underneath it."""
        if is_proven((self._verified.get(field) or {}).get(op)):
            return True
        return any(
            p.startswith(f"{field}.") and is_proven((f or {}).get(op))
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
    a = _Adapter(WISH, {"texts.intro": {"update": PROVEN}})
    assert _priority(a, "texts.intro") is None


def test_a_failed_probe_leaves_the_wish_alone() -> None:
    """`fail` is not proof that it works — quite the opposite."""
    a = _Adapter(WISH, {"texts.intro": {"update": "fail"}})
    assert _priority(a, "texts.intro") == {"update": "not writable upstream"}


def test_only_the_proven_op_is_dropped() -> None:
    a = _Adapter(
        {"texts.intro": {"create": "nope", "update": "nope"}},
        {"texts.intro": {"update": PROVEN}},
    )
    assert _priority(a, "texts.intro") == {"create": "nope"}


@pytest.mark.parametrize("weak", sorted(VERDICTS - {PROVEN}))
@pytest.mark.parametrize("op", ["read", "create", "update", "filter", "sort", "search"])
def test_no_weak_verdict_retires_a_wish(op: str, weak: str) -> None:
    """The rule this file exists for, stated once over the whole vocabulary rather
    than per facet. A weak verdict says the probe could not assert the claim — the
    previous design let three of those (`read` before the allowlist, then `search`
    and `sort` after it) delete hand-written backlog entries."""
    a = _Adapter({"texts.intro": {op: "upstream cannot"}}, {"texts.intro": {op: weak}})
    assert _priority(a, "texts.intro") == {op: "upstream cannot"}


def test_an_observed_read_now_does_retire_a_read_wish() -> None:
    """The flip side, and the reason the allowlist could go: `read: pass` no longer
    means "the schema declares it" but "a real record carried a value" — which is
    exactly what a read wish disputes, so it must count."""
    a = _Adapter(
        {"texts.intro": {"read": "upstream never fills this"}}, {"texts.intro": {"read": PROVEN}}
    )
    assert _priority(a, "texts.intro") is None


def test_a_container_counts_a_proven_leaf() -> None:
    """`items` is editable exactly when a line field has been shown to update."""
    a = _Adapter(
        {"items": {"update": "no line-item write path"}}, {"items.description": {"update": PROVEN}}
    )
    assert _priority(a, "items") is None


def test_the_reporter_names_what_the_stamp_skipped() -> None:
    """Dropping it from the view silently would be the same failure in reverse."""
    a = _Adapter(WISH, {"texts.intro": {"update": PROVEN}})
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
