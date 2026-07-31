"""Line-item totals and contribution margin on the four sales documents.

`items.totals` was declared on two of the four and populated on none — the read
stopped at taxRate, so every line reported no totals at all. Upstream carries both a
per-unit and a quantity-total revenue and the PUBLISHED spec has the two descriptions
the wrong way round (corrected in the monorepo, not yet in the spec repo), so the
mapping is pinned against what mvp actually returns:

    quantity 3 x net 100  ->  itemRevenue 100, lineItemRevenue 300

There is deliberately no per-line tax: v3 states none, and the core does not derive
one (ADR-014 is a 1:1 pass-through, and line-level rounding against the legacy PDF is
still open) — the gap is a blue wish instead.

`contributionMargin` is a PERCENT, not an amount — mvp returns 60 for net 100 / EK 40
and 75 for net 200 / EK 50, i.e. (net - EK) / net x 100. An absolute margin would
have been 60 and 150.
"""

from __future__ import annotations

from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

_ALL = (QuoteAdapter, SalesOrderAdapter, SalesInvoiceAdapter, CreditNoteAdapter)


def _raw(**line: Any) -> dict[str, Any]:
    base = {"id": 151013, "order": 1, "product": {"id": "61988"}, "quantity": 3}
    return {"id": 1, "financials": {"currency": "EUR"}, "lineItems": [{**base, **line}]}


def _item(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    return cls().map_read(raw)["items"][0]


def test_totals_come_from_the_quantity_total_not_the_per_unit_figure():
    """The trap: the published spec labels itemRevenue as the quantity total. It is
    not — mapping totals from it would report a third of the line on a quantity of 3."""
    raw = _raw(
        itemRevenue={
            "net": {"amount": "100.00000000", "currency": "EUR"},
            "gross": {"amount": "119.00000000", "currency": "EUR"},
        },
        lineItemRevenue={
            "net": {"amount": "300.00000000", "currency": "EUR"},
            "gross": {"amount": "357.00000000", "currency": "EUR"},
        },
    )
    for cls in _ALL:
        totals = _item(cls, raw)["totals"]
        assert totals["net"] == "300.00", cls.__name__
        assert totals["gross"] == "357.00", cls.__name__


def test_totals_are_none_when_upstream_reports_no_revenue():
    for cls in _ALL:
        assert _item(cls, _raw())["totals"] is None, cls.__name__


def test_totals_tolerate_a_missing_gross():
    raw = _raw(lineItemRevenue={"net": {"amount": "300.00", "currency": "EUR"}})
    for cls in _ALL:
        totals = _item(cls, raw)["totals"]
        assert totals["net"] == "300.00", cls.__name__
        assert totals["gross"] is None, cls.__name__


def test_contribution_margin_is_surfaced_as_the_percent_upstream_reports():
    for cls in _ALL:
        assert _item(cls, _raw(contributionMargin=75))["contributionMargin"] == 75, cls.__name__
        assert _item(cls, _raw())["contributionMargin"] is None, cls.__name__


def test_margin_is_read_only_and_totals_stay_read_only():
    for cls in _ALL:
        props = cls().fields()["items"]["node"]["properties"]
        assert props["contributionMargin"]["access"] == "readOnly", cls.__name__
        assert props["totals"]["access"] == "readOnly", cls.__name__
        # declared on all four now — it used to be missing on quote and creditNote
        assert set(props["totals"]["properties"]) == {"net", "gross"}, cls.__name__


def test_read_emitted_totals_and_margin_round_trip_without_a_wish():
    """Both are read-only, so writing a record straight back must not be refused."""
    raw = _raw(
        lineItemRevenue={"net": {"amount": "300.00", "currency": "EUR"}},
        contributionMargin=75,
    )
    for cls in _ALL:
        a = cls()
        item = a.map_read(raw)["items"][0]
        rej = {
            r for r in a.map_write({"items": [item]}, creating=True)[1] if r.startswith("items.")
        }
        assert rej == set(), f"{cls.__name__}: {rej}"


def test_no_tax_is_invented_for_a_line():
    """v3 states no per-line tax amount. Deriving gross - net would produce a money
    figure Xentral never reported, indistinguishable from a real one and free to
    disagree with the printed document (legacy rounding is an open question). The gap
    is carried as a blue wish, not filled in."""
    import json
    import pathlib

    raw = _raw(
        lineItemRevenue={
            "net": {"amount": "300.00", "currency": "EUR"},
            "gross": {"amount": "357.00", "currency": "EUR"},
        }
    )
    for cls in _ALL:
        totals = _item(cls, raw)["totals"]
        assert set(totals) == {"net", "gross"}, cls.__name__
        assert "tax" not in cls().fields()["items"]["node"]["properties"]["totals"]["properties"]

    prio = json.loads(
        (
            pathlib.Path(__file__).parent.parent / "cores/agentos_neo_xentral/priorities.json"
        ).read_text(encoding="utf-8")
    )
    for ent in ("Quote", "SalesOrder", "SalesInvoice", "CreditNote"):
        wishes = [w for w in prio["entities"][ent] if w["field"] == "items.totals.tax"]
        assert wishes, f"{ent}: the missing per-line tax must stay visible as a wish"
        assert wishes[0]["ops"] == ["read"], ent
