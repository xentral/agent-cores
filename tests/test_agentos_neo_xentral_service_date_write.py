"""Leistungsdatum (§14 UStG) on invoices and credit notes — two different truths.

`dates.serviceDate` was a blue wish on both entities. It stopped being one on the
INVOICE: v3 `UpdateInvoiceData`/`CreateInvoiceData` now carry `deliveryDate`, so the
adapter maps it instead of rejecting it. Rejecting it was also quietly breaking every
full-model PATCH, because the read emits `serviceDate` unconditionally and a
read-modify-write client echoes it straight back.

The CREDIT NOTE is the opposite case: its v3 DTOs still have no `deliveryDate`, and the
write silently dropped the field while the schema advertised it as writable. The write
behaviour is unchanged (still dropped — see the module docstring note), but the schema no
longer invites clients into that silence.

These pin the difference, so a later "make them consistent" refactor has to notice that
the inconsistency is the upstream's, not ours.
"""

from __future__ import annotations

from typing import Any

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter


def _invoice_write(model: dict[str, Any], *, creating: bool = False):
    return SalesInvoiceAdapter().map_write(model, creating=creating)


def test_invoice_service_date_maps_to_delivery_date():
    v3, rejected = _invoice_write({"dates": {"serviceDate": "2026-09-01"}})
    assert rejected == set()
    assert v3 == {"deliveryDate": "2026-09-01"}


def test_invoice_service_date_writable_on_create_too():
    v3, rejected = _invoice_write(
        {"customer": {"id": "cus_20201"}, "dates": {"serviceDate": "2026-09-01"}}, creating=True
    )
    assert rejected == set()
    assert v3["deliveryDate"] == "2026-09-01"


def test_invoice_service_date_can_be_cleared():
    # Nullable upstream: clearing is a valid edit, so a truthiness check would be wrong.
    v3, rejected = _invoice_write({"dates": {"serviceDate": None}})
    assert rejected == set()
    assert v3 == {"deliveryDate": None}


def test_invoice_round_trip_write_is_not_a_409():
    # The regression this replaces: the read always emits serviceDate, so echoing the
    # model back — the normal read-modify-write PATCH — used to 409 on every invoice.
    read_back = SalesInvoiceAdapter().map_read(
        {"id": 1, "documentDate": "2026-08-01", "deliveryDate": "2026-08-03"}
    )
    dates = read_back["dates"]
    v3, rejected = _invoice_write({"dates": dates})
    assert rejected == set()
    assert v3["deliveryDate"] == "2026-08-03"
    assert v3["documentDate"] == "2026-08-01"


def test_invoice_schema_advertises_service_date_as_writable():
    props = SalesInvoiceAdapter().fields()["dates"]["properties"]
    assert props["serviceDate"]["creatable"] and props["serviceDate"]["updatable"]


def test_credit_note_service_date_is_declared_read_only():
    # Its v3 DTOs still have no deliveryDate. The schema must say so rather than
    # advertising a write that the mapping drops on the floor.
    props = CreditNoteAdapter().fields()["dates"]["properties"]
    assert not props["serviceDate"].get("creatable")
    assert not props["serviceDate"].get("updatable")
    assert props["serviceDate"]["filterable"] is True  # still a useful filter
    # and the invoice's sibling really is the other way round
    inv = SalesInvoiceAdapter().fields()["dates"]["properties"]["serviceDate"]
    assert inv["updatable"] and not props["serviceDate"].get("updatable")
