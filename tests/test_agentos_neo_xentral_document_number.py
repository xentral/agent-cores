"""Document numbers: settable where Xentral takes one, refused loudly where it does not.

Three of the seven documents accept a number on create — verified in the monorepo
(`documentNumber` is declared on the Create*Data and applied by the create action:
normalizeDocumentNumber → uniqueness check → belegnr) and on mvp, where a sales
order created as ALT-70001 still carried it after actions/release. Omit it and the
configured number range draws the next one. The other four do not declare the field
at all and ignore a supplied value without an error.

Either way the core must not swallow it: `number` used to sit in every document's
`_IGNORE`, so a caller supplying one got a 201 and a different number with nothing
to see. Where upstream takes it we pass it through; where it cannot, the write is
refused with the reason.
"""

from __future__ import annotations

import json
import pathlib

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.delivery_note import DeliveryNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

# upstream declares AND applies documentNumber on create
_TAKES_IT = [
    (SalesOrderAdapter, "SalesOrder"),
    (SalesInvoiceAdapter, "SalesInvoice"),
    (CreditNoteAdapter, "CreditNote"),
]
# upstream does not declare it and ignores a supplied value
_DOES_NOT = [
    (QuoteAdapter, "Quote"),
    (PurchaseOrderAdapter, "PurchaseOrder"),
    (DeliveryNoteAdapter, "DeliveryNote"),
    (ReturnAdapter, "Return"),
]
_ALL = _TAKES_IT + _DOES_NOT


def _field_gaps() -> dict:
    """The parked field gaps per entity, out of backlog.yaml.

    The gaps left the specification when the core stopped rendering them: the spec now
    describes what the system IS, and the backlog is a plain work list nothing loads.
    These assertions still care that a recorded gap says what it always said.
    """

    import yaml

    path = pathlib.Path(__file__).parent.parent / "cores/agentos_neo_xentral/backlog.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---- where Xentral takes a number ---------------------------------------


def test_number_is_creatable_and_sent_as_document_number():
    for cls, name in _TAKES_IT:
        prop = cls().fields()["number"]
        assert prop.get("creatable") is True, name
        assert prop.get("access") != "readOnly", name
        body, rejected = cls().map_write({"number": "ALT-70001"}, creating=True)
        assert body["documentNumber"] == "ALT-70001", name
        assert rejected == set(), name


def test_number_is_not_updatable_and_says_so():
    """v3 PATCH has no slot for it, so a correction attempt must be reported rather
    than dropped — and the schema must not promise an edit that never happens."""
    for cls, name in _TAKES_IT:
        assert cls().fields()["number"].get("updatable") is not True, name
        body, rejected = cls().map_write({"number": "ALT-1"}, creating=False)
        assert "documentNumber" not in body, name
        assert "number" in rejected, name


def test_omitting_the_number_leaves_it_to_the_number_range():
    for cls, name in _TAKES_IT:
        body, rejected = cls().map_write({}, creating=True)
        assert "documentNumber" not in body, name
        assert rejected == set(), name


# ---- where it does not ---------------------------------------------------


def test_number_is_refused_where_upstream_ignores_it():
    """Upstream answers 201 and drops the value silently. Forwarding that would be
    the worst of both worlds — the caller believes the number landed."""
    for cls, name in _DOES_NOT:
        prop = cls().fields()["number"]
        assert prop.get("access") == "readOnly", name
        body, rejected = cls().map_write({"number": "ALT-70002"}, creating=True)
        assert "number" in rejected, name
        assert "documentNumber" not in body, name


# ---- never silently dropped, either way ---------------------------------


def test_number_left_the_ignore_set_everywhere():
    """`_IGNORE` is what makes a key vanish quietly."""
    for cls, name in _ALL:
        assert "number" not in cls._IGNORE, name


def test_the_backlog_keeps_only_the_correction_case():
    """Where upstream TAKES a number on create, the open ask is correcting it later —
    v3 PATCH has no slot for it.

    The other half is gone. Four documents ignore a supplied number silently, and the
    core refuses it for that reason (asserted above, in
    `test_number_is_refused_where_upstream_ignores_it`) — but the domain review decided
    that carrying a foreign number onto those four is not wanted, so the entries were
    dropped. The refusal stands; only the ASK went away."""
    gaps = _field_gaps()
    for _cls, name in _TAKES_IT:
        wishes = [w for w in gaps[name] if w["field"] == "number"]
        assert wishes and wishes[0]["ops"] == ["update"], name
        assert "PATCH has no slot" in wishes[0]["reason"], name
    for _cls, name in _DOES_NOT:
        assert not [w for w in gaps.get(name, []) if w["field"] == "number"], name


def test_the_409_refuses_and_names_the_fields():
    """A write naming a field the core does not write is refused, not silently dropped.

    The refusal is the load-bearing part and it stays: a 200 with the value gone
    destroys foreign keys on a migration without anyone noticing (ADR-014). What it no
    longer carries is a per-field reason. Those explained why a GAP existed, and the
    specification no longer records gaps — it describes what the system offers, so a
    field named here is one the caller invented or read from a stale schema.
    """
    import asyncio

    resp = asyncio.run(
        QuoteAdapter().request(
            method="POST",
            handle=None,
            query=[],
            body=json.dumps({"number": "ALT-70002"}).encode(),
            base_url="https://x.test",
            token="t",
            accept_language=None,
            client=None,
        )
    )
    assert resp.status_code == 409
    payload = json.loads(resp.content)
    assert payload["fields"] == ["number"]
    assert "reasons" not in payload, "the per-field reasons left with the gaps"
    assert "erp-spec.yaml" in payload["detail"], "the caller is pointed at the model"


def test_a_normal_create_is_unaffected():
    body, rejected = SalesOrderAdapter().map_write({"customer": "cus_3"}, creating=True)
    assert rejected == set()
    assert body
