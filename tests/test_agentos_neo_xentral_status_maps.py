"""Every status value upstream sends has a place to land.

`status_map` falls back to the entity's default when it does not know a value.
That is the right behaviour for an unknown one and a silent lie for a value the
upstream sends routinely. Measured on mvp before this was fixed:

    deliveryNotes  upstream {'released': 52, 'cancelled': 4, 'sent': 44}
                   core     {'picking': 52, 'cancelled': 4, 'draft': 44}

44 dispatched delivery notes read as drafts, and all 35 `done` returns read as
still requested. The Quote map had the same hole for `commissioned` — the state
605,663 offers are in across all tenants — but mvp happens to carry none of them,
so no probe could ever have caught it. That is why the vocabularies below are
pinned against the upstream enums rather than against test-instance data.

Sources (monorepo `app/Modules/ERP/.../Enums/`): OfferStatus, SalesOrderStatus,
InvoiceStatus, CreditNoteStatus, DeliveryNoteStatus, PurchaseOrderStatus,
ReturnOrderProgress.
"""

from __future__ import annotations

import importlib

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.base import STATUS_FALLBACKS, status_map

# module → (map attribute, every value the upstream enum can emit)
UPSTREAM = {
    "quote": (
        "_STATUS",
        {
            "draft",
            "commissioned",
            "completed",
            "ordered",
            "released",
            "accepted",
            "declined",
            "expired",
            "sent",
            "cancelled",
        },
    ),
    "sales_order": ("_STATUS", {"draft", "released", "completed", "cancelled", "sent"}),
    "sales_invoice": ("_STATUS", {"draft", "released", "sent", "cancelled", "completed"}),
    "credit_note": ("_STATUS", {"draft", "released", "completed", "cancelled", "sent"}),
    "delivery_note": ("_STATUS", {"draft", "released", "sent", "cancelled", "completed"}),
    "purchase_order": ("_STATUS", {"draft", "released", "sent", "completed", "cancelled"}),
    "return_order": ("_PROGRESS", {"announced", "received", "checked", "done"}),
}


def _map(module: str, attr: str) -> dict[str, str]:
    mod = importlib.import_module(f"xentral_entity_cores.agentos_neo_xentral.emulated.{module}")
    return getattr(mod, attr)


@pytest.mark.parametrize(("module", "spec"), UPSTREAM.items(), ids=list(UPSTREAM))
def test_every_upstream_value_is_mapped(module: str, spec: tuple[str, set[str]]) -> None:
    attr, values = spec
    missing = sorted(values - set(_map(module, attr)))
    assert missing == [], f"{module}.{attr} would report these as its default: {missing}"


@pytest.mark.parametrize(("module", "spec"), UPSTREAM.items(), ids=list(UPSTREAM))
def test_every_mapped_value_lands_in_the_declared_vocabulary(
    module: str, spec: tuple[str, set[str]]
) -> None:
    """A map entry pointing at a value the schema does not offer would be just as
    unreadable as the fallback."""
    mod = importlib.import_module(f"xentral_entity_cores.agentos_neo_xentral.emulated.{module}")
    # Return's map is `_PROGRESS` but its option list is still `_STATUS_OPTIONS`.
    declared = getattr(mod, f"{spec[0]}_OPTIONS", None) or getattr(mod, "_STATUS_OPTIONS", [])
    options = {o["value"] for o in declared if isinstance(o, dict)}
    assert options, f"{module} declares no options"
    stray = sorted(set(_map(module, spec[0]).values()) - options)
    assert stray == [], stray


# the three that were actually wrong


def test_a_dispatched_delivery_note_is_shipped_not_draft() -> None:
    assert _map("delivery_note", "_STATUS")["sent"] == "shipped"


def test_a_finished_return_is_settled_not_requested() -> None:
    assert _map("return_order", "_PROGRESS")["done"] == "settled"


@pytest.mark.parametrize("value", ["commissioned", "ordered"])
def test_a_commissioned_quote_is_accepted_not_draft(value: str) -> None:
    """`commissioned` (beauftragt) is the operational accepted state — the tenant
    audit counts 605,663 of them against zero `angenommen`."""
    assert _map("quote", "_STATUS")[value] == "accepted"


# the recorder that makes the next one visible


def test_an_unknown_value_is_recorded_with_what_replaced_it() -> None:
    STATUS_FALLBACKS.clear()
    assert status_map({"draft": "draft"}, "surprise", "draft") == "draft"
    assert STATUS_FALLBACKS == {("surprise", "draft")}


def test_a_known_value_records_nothing() -> None:
    STATUS_FALLBACKS.clear()
    assert status_map({"sent": "shipped"}, "sent", "draft") == "shipped"
    assert STATUS_FALLBACKS == set()


def test_without_a_default_the_raw_value_passes_through_unrecorded() -> None:
    """Nothing is hidden, so there is nothing to report."""
    STATUS_FALLBACKS.clear()
    assert status_map({"a": "b"}, "surprise") == "surprise"
    assert STATUS_FALLBACKS == set()


def test_an_empty_value_is_not_a_miss() -> None:
    STATUS_FALLBACKS.clear()
    assert status_map({"a": "b"}, None, "draft") == "draft"
    assert status_map({"a": "b"}, "", "draft") == "draft"
    assert STATUS_FALLBACKS == set()
