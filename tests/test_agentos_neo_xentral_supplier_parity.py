"""Supplier gets what Customer has, because upstream gives them the same thing.

v3 treats the two partners symmetrically: identical route sets (15 each, including
`DELETE /{id}`, contactPersons and deliveryAddresses), and `SupplierResource` uses
the very same `OutputCustomFields` trait as `CustomerResource`. The asymmetry was
ours — Supplier was derived from Customer and a few blocks were left behind:

* `delete` was missing from the manifest, so the core answered its own 405 while
  upstream happily deletes (measured on mvp: `DELETE /api/v3/suppliers/20464` →
  204, then 404). The verify run read that 405 as an upstream refusal and left a
  `VT Verify GmbH` behind on every single run.
* `customFields` was declared as an empty embedded blob that could only ever read
  `{}` — worse than absent, because it looks like an answer.
* `notes` was not modelled at all, though it is readable and writable (measured:
  PATCH 200, value sticks).
* `finance.onHold` — the delivery block. Customer reads it from
  `fulfillment.deliveryBlock`; Supplier reported nothing.

The free-field mapping now lives in `base.custom_fields_to_v3` rather than on the
Customer adapter, so the two cannot drift apart again.
"""

from __future__ import annotations

from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.base import custom_fields_to_v3
from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter


def _spec(adapter: Any, path: str) -> dict[str, Any]:
    node = adapter.fields()
    spec: dict[str, Any] = {}
    for part in path.split("."):
        spec = node[part]
        node = spec.get("properties") or (spec.get("node") or {}).get("properties") or {}
    return spec


def test_delete_is_offered_because_upstream_offers_it() -> None:
    """The core's own 405 read as an upstream refusal for months."""
    assert "delete" in SupplierAdapter.manifest.operations
    assert set(SupplierAdapter.manifest.operations) == set(CustomerAdapter.manifest.operations)


@pytest.mark.parametrize("path", ["notes", "customFields", "customFields.value"])
def test_the_partner_fields_match_the_customer_side(path: str) -> None:
    cu, su = _spec(CustomerAdapter(), path), _spec(SupplierAdapter(), path)
    for flag in ("type", "creatable", "updatable", "access"):
        assert cu.get(flag) == su.get(flag), f"{path}.{flag}"


def test_custom_fields_are_a_typed_collection_not_an_empty_blob() -> None:
    """It used to be `prop("embedded", …, properties={})` — a field that could only
    ever read `{}`, which reads like "this supplier has none"."""
    spec = _spec(SupplierAdapter(), "customFields")
    assert spec["type"] == "collection"
    assert set(spec["node"]["properties"]) == {"key", "label", "type", "value"}


def test_the_include_asks_for_them() -> None:
    """Without the include, upstream leaves customFields out entirely."""
    assert "customFields" in SupplierAdapter.include


# ---- read ----------------------------------------------------------------

_RAW = {
    "id": "8",
    "name": "ACME Supplies",
    "notes": "ruft immer freitags an",
    "fulfillment": {"deliveryBlock": True},
    "customFields": [{"key": "cf1", "label": "Region", "type": "text", "value": "Süd"}],
}


def test_notes_and_custom_fields_are_read() -> None:
    got = SupplierAdapter().map_read(_RAW)
    assert got["notes"] == "ruft immer freitags an"
    assert got["customFields"] == [
        {"key": "cf1", "label": "Region", "type": "text", "value": "Süd"}
    ]


def test_the_delivery_block_is_read_from_the_same_place_as_the_customer_side() -> None:
    assert SupplierAdapter().map_read(_RAW)["finance"]["onHold"] is True
    assert CustomerAdapter().map_read(_RAW)["finance"]["onHold"] is True


def test_a_supplier_without_the_block_reports_none_not_a_guess() -> None:
    assert SupplierAdapter().map_read({"id": "8"})["finance"]["onHold"] is None


# ---- write ---------------------------------------------------------------


def test_notes_reach_the_wire() -> None:
    body, rejected = SupplierAdapter().map_write({"notes": "neu"}, creating=False)
    assert body["notes"] == "neu"
    assert rejected == set()


def test_custom_fields_reach_the_wire_in_the_upstream_shape() -> None:
    body, rejected = SupplierAdapter().map_write(
        {"customFields": [{"key": "cf1", "label": "Region", "value": "Nord"}]}, creating=False
    )
    assert body["customFields"] == [{"key": "cf1", "label": "Region", "value": "Nord"}]
    assert rejected == set()


def test_a_row_without_a_label_is_refused_not_sent() -> None:
    """Upstream requires the label; sending without it earns a 400 the caller
    cannot read."""
    _body, rejected = SupplierAdapter().map_write(
        {"customFields": [{"key": "cf1", "value": "Nord"}]}, creating=False
    )
    assert "customFields" in rejected


def test_both_partners_use_one_mapping() -> None:
    """The helper moved to base so the two cannot drift apart."""
    rows = [{"key": "k", "label": "L", "value": "v"}]
    assert custom_fields_to_v3(rows) == [{"key": "k", "label": "L", "value": "v"}]
    cu, _ = CustomerAdapter().map_write({"customFields": rows}, creating=False)
    su, _ = SupplierAdapter().map_write({"customFields": rows}, creating=False)
    assert cu["customFields"] == su["customFields"]
