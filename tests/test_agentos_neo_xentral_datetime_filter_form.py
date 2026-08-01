"""A datetime filter goes out in the form its collection accepts.

The two upstream families disagree, and neither can be derived from the schema —
both declare `createdAt` as a filterable datetime and both RETURN a full ISO
timestamp on read. Measured on mvp:

    /api/v3/customers    2024-11-21T04:16:22+01:00 -> 400 "not a valid date"
                         2024-11-21                -> 200
    /api/v3/salesOrders  2024-11-21T04:16:22+01:00 -> 200
                         2024-11-21                -> 400 "not a valid datetime"

So on the partner endpoints a caller cannot filter on the value they were just
handed. That is an upstream inconsistency and is reported as such; until it is
fixed the facade absorbs it, because the model promises ONE `createdAt: datetime,
filterable` across every entity.

This is format adaptation at the boundary, not invented data — the distinction
ADR-014 draws. The value still comes from the caller; only its shape changes.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.customer import CustomerAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter

_STAMP = "2024-11-21T04:16:22+01:00"
_DATE = "2024-11-21"


def _filter_params(adapter, key: str, value: str) -> dict[str, str]:
    params = [
        ("page[size]", "5"),
        ("filter[0][key]", key),
        ("filter[0][op]", "equals"),
        ("filter[0][value]", value),
    ]
    return dict(adapter._strip_reference_filter_prefixes(params))


@pytest.mark.parametrize("adapter_cls", [CustomerAdapter, SupplierAdapter])
@pytest.mark.parametrize("key", ["createdAt", "updatedAt"])
def test_partner_collections_get_the_date_part(adapter_cls: type, key: str) -> None:
    out = _filter_params(adapter_cls(), key, _STAMP)
    assert out["filter[0][value]"] == _DATE


@pytest.mark.parametrize("adapter_cls", [SalesOrderAdapter, QuoteAdapter])
@pytest.mark.parametrize("key", ["createdAt", "updatedAt"])
def test_document_collections_keep_the_full_timestamp(adapter_cls: type, key: str) -> None:
    """The opposite family must stay untouched — trimming here would break it."""
    out = _filter_params(adapter_cls(), key, _STAMP)
    assert out["filter[0][value]"] == _STAMP


def test_only_datetime_keys_are_trimmed() -> None:
    """A string that merely contains a T must survive: the trim is keyed off the
    schema type, not off the value's shape."""
    a = CustomerAdapter()
    out = _filter_params(a, "name", "ACME T-Shirts GmbH")
    assert out["filter[0][value]"] == "ACME T-Shirts GmbH"


def test_a_date_only_value_passes_through_unchanged() -> None:
    out = _filter_params(CustomerAdapter(), "createdAt", _DATE)
    assert out["filter[0][value]"] == _DATE


def test_reference_prefix_stripping_still_works() -> None:
    """The trim shares its pass over the params with the speaking-id stripping —
    neither may swallow the other."""
    out = _filter_params(SalesOrderAdapter(), "customer", "cus_20423")
    assert out["filter[0][value]"] == "20423"


def test_the_flag_is_set_only_where_it_was_measured() -> None:
    assert CustomerAdapter.datetime_filters_take_date_only is True
    assert SupplierAdapter.datetime_filters_take_date_only is True
    assert SalesOrderAdapter.datetime_filters_take_date_only is False
    assert QuoteAdapter.datetime_filters_take_date_only is False
