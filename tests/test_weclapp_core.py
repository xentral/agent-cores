"""weclapp_core — OpenAPI→Entity generator + adapter build (no tenant).

Exercises the generator mapping against the committed sample spec and confirms the
generated entities plug into the shared engine (metadata renders).
"""

from __future__ import annotations

from xentral_entity_cores.weclapp_core.entities import build_adapters

ADAPTERS = {a.manifest.key: a for a in build_adapters()}


def _entity(key):
    return ADAPTERS[key].entity


def _field(entity, out_key):
    for f in (*entity.scalars, *entity.references):
        if f.out_key() == out_key:
            return f
    raise KeyError(out_key)


def test_roster_is_top_level_entities_only():
    # SalesOrder / Party / Article are entities; OrderItem / Address are nested-only.
    assert set(ADAPTERS) == {"SalesOrder", "Party", "Article"}
    assert _entity("SalesOrder").endpoint == "salesOrder"
    assert _entity("Party").endpoint == "party"
    assert _entity("Article").endpoint == "article"
    for a in ADAPTERS.values():
        assert a.manifest.operations == ("list", "read")  # read-only v1


def test_native_names_are_verbatim():
    so = _entity("SalesOrder")
    # weclapp's own names, not the curated documentNumber/documentDate/party
    assert _field(so, "salesOrderNumber").type == "string"
    assert {f.out_key() for f in so.scalars} >= {
        "salesOrderNumber",
        "commissionNumber",
        "netAmount",
    }


def test_epoch_dates_and_enums():
    so = _entity("SalesOrder")
    order_date = _field(so, "orderDate")
    assert order_date.type == "datetime" and order_date.epoch is True
    status = _field(so, "status")
    assert status.type == "select"
    assert {v for v, _ in status.options} == {
        "ORDER_ENTRY_IN_PROGRESS",
        "ORDER_CONFIRMATION_PRINTED",
        "SHIPPING_STARTED",
        "COMPLETED",
    }


def test_foreign_key_references_with_party_alias():
    so = _entity("SalesOrder")
    # customerId -> reference; the polymorphic party alias maps it to "party"
    cust = next(r for r in so.references if r.out_key() == "customerId")
    assert cust.reference == "party"


def test_order_items_collection():
    so = _entity("SalesOrder")
    items = next(c for c in so.collections if c.out_key() == "orderItems")
    sub = {f.out_key(): f for f in items.fields}
    assert "id" not in sub  # engine adds the item id itself
    assert sub["quantity"].type == "decimal"
    # articleId inside the line item is a reference to Article
    assert sub["articleId"].reference == "Article"


def test_embedded_address():
    so = _entity("SalesOrder")
    addr = next(e for e in so.embeds if e.out_key() == "deliveryAddress")
    assert {f.out_key() for f in addr.fields} >= {"city", "countryCode", "street1"}


def test_metadata_renders_through_shared_engine():
    meta = ADAPTERS["SalesOrder"].metadata()
    assert meta["key"] == "SalesOrder"
    assert meta["origin"] == "emulated"
    props = meta["rootNode"]["properties"]
    assert props["customerId"]["type"] == "reference"
    assert props["orderItems"]["type"] == "collection"
    assert props["orderItems"]["node"]["properties"]["articleId"]["type"] == "reference"
    assert props["deliveryAddress"]["type"] == "embedded"
    # read-only mirror: scalars carry no write access
    assert props["salesOrderNumber"].get("access") == "readOnly"


def test_party_shape():
    party = _entity("Party")
    assert _field(party, "partyType").type == "select"
    assert _field(party, "customer").type == "boolean"
    created = _field(party, "createdDate")
    assert created.type == "datetime" and created.epoch is True
