"""The main address must survive a read-edit-write.

The model folds three upstream places into ONE `addresses` collection: the
default row is the record's `primaryAddress`, a billing row is
`deviatingBillingAddress`, shipping rows are a sub-resource. The mixin routes
them back on write.

The default row lost four fields on the way out. The router forwarded six of the
ten content fields the read side emits, so `contactPerson`, `gln`, `email` and
`phone` were dropped for the MAIN address while the billing row kept them —
silently, on a plain round-trip. Upstream `primaryAddress` accepts all four
(UpdateCustomerData, documents spec).
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.partner_subresources import (
    _MAIN_ADDRESS_KEYS,
)
from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter

ROW = {
    "name": "Acme GmbH",
    "contactPerson": "Laura Schneider",
    "street": "Innovationsallee 7",
    "zip": "80333",
    "city": "München",
    "state": "BY",
    "country": "DE",
    "gln": "4012345000300",
    "email": "l.schneider@acme.de",
    "phone": "+4915112345670",
}


@pytest.fixture(params=[CustomerAdapter, SupplierAdapter], ids=["Customer", "Supplier"])
def adapter(request):
    return request.param.__new__(request.param)


def test_every_field_of_the_row_reaches_upstream(adapter):
    body, rejected = adapter.map_write({"primaryAddress": ROW}, creating=False)
    assert not rejected
    assert body["primaryAddress"] == {
        "name": "Acme GmbH",
        "contactPerson": "Laura Schneider",
        "street": "Innovationsallee 7",
        "zipCode": "80333",  # the model calls it `zip`
        "city": "München",
        "state": "BY",
        "country": "DE",
        "gln": "4012345000300",
        "email": "l.schneider@acme.de",
        "phone": "+4915112345670",
    }


@pytest.mark.parametrize("field", ["contactPerson", "gln", "email", "phone"])
def test_the_four_that_used_to_vanish(adapter, field):
    """Each was accepted, dropped, and reported as a successful write."""
    body, _ = adapter.map_write({"primaryAddress": {field: ROW[field]}}, creating=False)
    assert field in body["primaryAddress"]


def test_the_router_forwards_what_the_read_side_emits(adapter):
    """The asymmetry is the bug: anything readable on the row must be writable
    on it, or a round-trip quietly loses data."""
    record = adapter.map_read(
        {"id": 3, "primaryAddress": {"name": "x"}, "communication": {}, "financials": {}}
    )
    main = next((a for a in (record.get("addresses") or []) if a.get("isDefault")), None)
    assert main is not None
    content = {k for k in main if k not in ("id", "type", "label", "isDefault")}
    assert content == set(_MAIN_ADDRESS_KEYS)


def test_the_address_row_wins_over_the_flat_field(adapter):
    """`email`/`phone` exist BOTH flat on the record and on the row. The row is
    the more specific statement, so it must not be overwritten by the flat one."""
    body, _ = adapter.map_write(
        {"email": "flat@acme.de", "primaryAddress": {"email": "row@acme.de"}}, creating=False
    )
    assert body["primaryAddress"]["email"] == "row@acme.de"
