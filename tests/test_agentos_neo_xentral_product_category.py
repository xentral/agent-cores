"""ProductCategory on the entity API: the posting accounts are the point.

A Warengruppe was a read-only lookup here with four fields — enough to name a
group, not enough to say what it does. What it actually carries is the bridge
from a product to the general ledger: sixteen accounts, revenue and expense,
split by the tax situation of the transaction.

Two things are pinned here because they are easy to get wrong in the same way:

* the asymmetry is upstream's. Revenue has an ``export`` account and no
  ``import``; expense has ``import`` and no ``export``. Inventing the missing
  halves would imply postings that cannot exist, so a caller who puts one on the
  wrong side is told rather than silently ignored.
* ``parentId`` is an integer upstream and is NOT filterable — the model keeps a
  reference outward and says so in the field description instead of advertising
  a filter that answers 422.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.emulated.product_category import (
    ProductCategoryAdapter,
)

_ROW = {
    "id": "7",
    "uuid": "u-cat",
    "name": "Handelsware",
    "parentId": 1,
    "nextNumber": "100023",
    "isUsingMainProductNumberRange": False,
    "revenueAccountDomesticStandardTax": "8400",
    "revenueAccountDomesticExport": "8120",
    "expenseAccountDomesticStandardTax": "3400",
    "expenseAccountDomesticImport": "3550",
    "taxTextExport": "Steuerfreie Ausfuhrlieferung",
}


def test_the_ledger_bridge_is_readable_at_all():
    d = ProductCategoryAdapter().map_read(_ROW)
    assert d["accounts"]["revenue"]["standard"] == "8400"
    assert d["accounts"]["revenue"]["export"] == "8120"
    assert d["accounts"]["expense"]["standard"] == "3400"
    assert d["accounts"]["expense"]["import"] == "3550"
    assert d["taxTexts"]["export"] == "Steuerfreie Ausfuhrlieferung"


def test_all_sixteen_accounts_are_writable():
    a = ProductCategoryAdapter()
    revenue = {k: "8000" for k in a.fields()["accounts"]["properties"]["revenue"]["properties"]}
    expense = {k: "3000" for k in a.fields()["accounts"]["properties"]["expense"]["properties"]}
    assert len(revenue) == 8 and len(expense) == 8
    wire, rejected = a.map_write(
        {"accounts": {"revenue": revenue, "expense": expense}}, creating=True
    )
    assert not rejected
    assert sum(1 for k in wire if k.startswith(("revenueAccount", "expenseAccount"))) == 16


def test_the_sides_are_not_interchangeable():
    """Revenue has no import account and expense has no export one — upstream
    cannot post either, so a caller must hear about it."""
    _wire, rejected = ProductCategoryAdapter().map_write(
        {"accounts": {"revenue": {"import": "3550"}, "expense": {"export": "8120"}}},
        creating=True,
    )
    assert rejected == {"accounts.revenue.import", "accounts.expense.export"}


def test_the_parent_travels_as_a_bare_integer():
    wire, _ = ProductCategoryAdapter().map_write({"parent": {"id": "pcat_1"}}, creating=True)
    assert wire["parentId"] == 1
    assert isinstance(wire["parentId"], int)


def test_the_parent_is_not_advertised_as_filterable():
    """`filter[parentId]` answers 422 "Property 'parentId' is not filterable"."""
    spec = ProductCategoryAdapter().fields()["parent"]
    assert not spec.get("filterable")
    assert "not filterable" in (spec.get("description") or "")


def test_the_number_range_round_trips():
    a = ProductCategoryAdapter()
    d = a.map_read(_ROW)
    assert d["numberRange"] == {"usesMainRange": False, "nextNumber": "100023"}
    wire, _ = a.map_write(
        {"numberRange": {"usesMainRange": True, "nextNumber": "900001"}}, creating=False
    )
    assert wire["isUsingMainProductNumberRange"] is True
    assert wire["nextNumber"] == "900001"


def test_the_record_is_addressed_by_uuid():
    """The old lookup emitted pcat_<numeric>; the entity API reads by uuid and
    does not allow filtering on id, so a numeric handle cannot be resolved."""
    assert ProductCategoryAdapter().map_read(_ROW)["id"] == "pcat_u-cat"
    assert (
        ProductCategoryAdapter()._created_handle({"data": {"id": "7", "uuid": "u-cat"}}) == "u-cat"
    )


def test_it_kept_its_place_in_the_settings_group():
    from xentral_entity_cores.agentos_neo_xentral.emulated.settings import SETTINGS_ADAPTERS

    a = next(x for x in SETTINGS_ADAPTERS if x.manifest.key == "ProductCategory")
    assert a.manifest.category == "settings"
    assert {"create", "update", "delete"} <= set(a.manifest.operations)
