"""Regression: Product ``map_read`` must survive scalar upstream shapes.

Xentral v1 carries ``manufacturer`` as a free-text string on many tenants (the
``{name, link}`` object shape is not guaranteed per record), and computed
blocks like ``calculatedPurchasePrice`` are not reliably objects either. A
single such record used to crash the whole product list
(``AttributeError: 'str' object has no attribute 'get'``) — which took down
every record picker targeting Product.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter


def test_manufacturer_as_string_reads_as_name():
    model = ProductAdapter().map_read({"id": 1, "manufacturer": "ACME GmbH"})
    assert model["manufacturer"] == {"name": "ACME GmbH", "website": None}


def test_manufacturer_empty_string_reads_as_none():
    model = ProductAdapter().map_read({"id": 1, "manufacturer": ""})
    assert model["manufacturer"]["name"] is None


def test_manufacturer_object_shape_still_maps():
    model = ProductAdapter().map_read(
        {"id": 1, "manufacturer": {"name": "ACME", "link": "https://acme.example"}}
    )
    assert model["manufacturer"] == {"name": "ACME", "website": "https://acme.example"}


def test_scalar_calculated_purchase_price_does_not_crash():
    model = ProductAdapter().map_read({"id": 1, "calculatedPurchasePrice": "12.34"})
    assert model["prices"]["purchase"] is None or isinstance(model["prices"]["purchase"], dict)
