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


def test_scalar_measurements_read_as_dimensions():
    """The reshipped v3 products ship bare numbers — these used to crash."""
    model = ProductAdapter().map_read(
        {"id": 1, "measurements": {"length": 30, "width": 20, "height": 10}}
    )
    assert model["logistics"]["dimensions"] == {
        "length": 30,
        "width": 20,
        "height": 10,
        "unit": "cm",
    }


def test_object_measurements_still_map():
    model = ProductAdapter().map_read(
        {
            "id": 1,
            "measurements": {
                "length": {"value": 30, "unit": "mm"},
                "width": {"value": 20, "unit": "mm"},
                "height": {"value": 10, "unit": "mm"},
            },
        }
    )
    assert model["logistics"]["dimensions"] == {
        "length": 30,
        "width": 20,
        "height": 10,
        "unit": "mm",
    }


def test_mixed_measurement_shapes_do_not_crash():
    """Nothing guarantees all three axes share one generation."""
    model = ProductAdapter().map_read(
        {"id": 1, "measurements": {"length": 30, "width": {"value": 20, "unit": "cm"}}}
    )
    assert model["logistics"]["dimensions"] == {
        "length": 30,
        "width": 20,
        "height": None,
        "unit": "cm",
    }


def test_missing_length_yields_no_dimensions():
    model = ProductAdapter().map_read({"id": 1, "measurements": {"width": 20}})
    assert model["logistics"]["dimensions"] is None


def test_scalar_measurements_do_not_disturb_weight():
    """weight/netWeight keep their own unit even when dimensions are scalars."""
    model = ProductAdapter().map_read({"id": 1, "measurements": {"length": 30, "weight": 2.5}})
    assert model["logistics"]["weight"] == {"value": 2.5, "unit": "kg"}
