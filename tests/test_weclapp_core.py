"""weclapp_core — OpenAPI→Entity generator + adapter build (no tenant).

The mapping is tested deterministically against the committed sample fragment
(weclapp's real spec shape); a smoke test then runs the generator over the full
committed spec (openapi.json) to confirm it produces the native mirror.
"""

from __future__ import annotations

import json
from pathlib import Path

from xentral_entity_cores.weclapp_core.entities import build_adapters
from xentral_entity_cores.weclapp_core.generator import build_entities_from_openapi

_PKG = Path(__file__).resolve().parents[1] / "cores" / "weclapp_core"
_SAMPLE = json.loads((_PKG / "openapi.sample.json").read_text())
ENTITIES = {e.key: e for e in build_entities_from_openapi(_SAMPLE)}


def _field(entity, out_key):
    for f in (*entity.scalars, *entity.references):
        if f.out_key() == out_key:
            return f
    raise KeyError(out_key)


# ---- generator mapping (deterministic, against the sample) ------------------


def test_entities_from_list_paths_only():
    # salesOrder/party/article have GET /{name} paths; salesOrderItem/recordAddress
    # are nested-only and must not become entities. Keys are weclapp names verbatim.
    assert set(ENTITIES) == {"salesOrder", "party", "article"}
    assert ENTITIES["salesOrder"].endpoint == "salesOrder"


def test_operations_and_writability_from_paths():
    so = ENTITIES["salesOrder"]  # sample paths give it post/put/delete
    assert so.operations == ("list", "read", "create", "update", "delete")
    # article/party sample paths are GET-only -> read-only
    assert ENTITIES["article"].operations == ("list", "read")
    # a business field is writable; system-managed fields are not
    assert _field(so, "orderNumber").writable is True
    # collections and their business sub-fields are writable (weclapp accepts
    # nested items on the entity's own POST/PUT); system fields stay read-only
    items = next(c for c in so.collections if c.out_key() == "orderItems")
    assert items.writable is True
    sub = {f.out_key(): f for f in items.fields}
    assert sub["quantity"].writable is True
    assert sub["articleId"].writable is True


def test_native_names_verbatim():
    so = ENTITIES["salesOrder"]
    assert _field(so, "orderNumber").type == "string"  # not the curated "documentNumber"


def test_timestamp_dates_and_number_decimals():
    so = ENTITIES["salesOrder"]
    od = _field(so, "orderDate")
    assert od.type == "datetime" and od.epoch is True  # format: timestamp
    assert _field(so, "netAmount").type == "decimal"  # type:string, format:number


def test_enum_becomes_select():
    status = _field(ENTITIES["salesOrder"], "status")
    assert status.type == "select"
    assert {v for v, _ in status.options} == {
        "ORDER_ENTRY_IN_PROGRESS",
        "ORDER_CONFIRMATION_PRINTED",
        "CLOSED",
        "CANCELLED",
    }


def test_x_related_entity_name_reference():
    so = ENTITIES["salesOrder"]
    cust = next(r for r in so.references if r.out_key() == "customerId")
    assert cust.reference == "party"  # from x-relatedEntityName (polymorphic party)


def test_collection_and_nested_reference():
    so = ENTITIES["salesOrder"]
    items = next(c for c in so.collections if c.out_key() == "orderItems")
    sub = {f.out_key(): f for f in items.fields}
    assert "id" not in sub
    assert sub["quantity"].type == "decimal"
    assert sub["articleId"].reference == "article"


def test_embedded_address():
    addr = next(e for e in ENTITIES["salesOrder"].embeds if e.out_key() == "deliveryAddress")
    assert {f.out_key() for f in addr.fields} >= {"city", "countryCode", "street1"}


def test_metadata_renders_through_shared_engine():
    # render via an adapter built from the sample entity + the shared engine
    from xentral_entity_cores.agentos_neo_weclapp.emulated.base import WeclappAdapterBase

    props = WeclappAdapterBase(ENTITIES["salesOrder"]).metadata()["rootNode"]["properties"]
    assert props["customerId"]["type"] == "reference"
    assert props["orderItems"]["type"] == "collection"
    assert props["orderItems"]["node"]["properties"]["articleId"]["type"] == "reference"
    assert props["deliveryAddress"]["type"] == "embedded"
    # writable business fields and collections carry no read-only access flag
    assert "access" not in props["orderNumber"]
    assert "access" not in props["orderItems"]
    assert "access" not in props["orderItems"]["node"]["properties"]["quantity"]
    # embeds stay read-only
    assert props["deliveryAddress"]["access"] == "readOnly"


def test_string_array_becomes_writable_tag_field():
    tags = _field(ENTITIES["salesOrder"], "tags")
    assert tags.type == "tag"
    assert tags.writable is True


def test_write_payload_serialises_collections_and_tags():
    from xentral_entity_cores.agentos_neo_weclapp.emulated.base import write_payload

    so = ENTITIES["salesOrder"]
    wire = write_payload(
        so,
        {
            "customerId": {"id": "4264"},
            "tags": ["claude-fuchs-0725"],
            "orderItems": [
                # new item: no id — created by weclapp
                {"articleId": {"id": "4269"}, "quantity": "7", "unitPrice": "19.75"},
                # existing item: id/version pass through so weclapp updates it
                {"id": "99", "version": "2", "quantity": "1"},
            ],
            "orderDate": "2026-07-25",  # epoch field — converted back to ms
            "createdDate": "2026-07-25T00:00:00Z",  # system field — dropped
            "unknown": "x",  # unknown key — dropped
        },
    )
    assert wire["customerId"] == "4264"
    assert wire["tags"] == ["claude-fuchs-0725"]
    assert wire["orderItems"] == [
        {"articleId": "4269", "quantity": "7", "unitPrice": "19.75"},
        {"id": "99", "version": "2", "quantity": "1"},
    ]
    assert isinstance(wire["orderDate"], int)
    assert "createdDate" not in wire
    assert "unknown" not in wire


def test_update_merges_collection_items_by_id():
    from xentral_entity_cores.agentos_neo_weclapp.emulated.base import (
        _merge_collection_items,
    )

    current = [
        {"id": "4280", "version": "0", "quantity": "7", "itemType": "DEFAULT", "taxId": "3681"},
        {"id": "4281", "version": "1", "quantity": "1", "itemType": "DEFAULT", "taxId": "3681"},
    ]
    merged = _merge_collection_items(
        current,
        [
            # partial update: overlaid onto the current raw item (weclapp PUT
            # replaces items completely, so the merge supplies the rest)
            {"id": "4280", "manualUnitPrice": True, "unitPrice": "19.75"},
            # new item: sent as-is
            {"articleId": "9", "quantity": "2"},
            # item 4281 omitted -> stays omitted (weclapp deletes it)
        ],
    )
    assert merged == [
        {
            "id": "4280",
            "version": "0",
            "quantity": "7",
            "itemType": "DEFAULT",
            "taxId": "3681",
            "manualUnitPrice": True,
            "unitPrice": "19.75",
        },
        {"articleId": "9", "quantity": "2"},
    ]


def test_write_payload_drops_embeds_and_readonly_collections():
    from xentral_entity_cores.agentos_neo_weclapp.emulated.base import (
        Collection,
        write_payload,
    )

    so = ENTITIES["salesOrder"]
    # embeds are never written
    assert "deliveryAddress" not in write_payload(so, {"deliveryAddress": {"city": "A"}})
    # a non-writable collection is dropped even when supplied
    import dataclasses

    ro = Collection("orderItems", label="orderItems", fields=())
    entity = dataclasses.replace(so, collections=(ro,))
    assert "orderItems" not in write_payload(entity, {"orderItems": [{"quantity": "1"}]})


# ---- smoke test over the full committed spec (openapi.json) ------------------


def test_full_spec_generates_the_native_mirror():
    adapters = {a.manifest.key: a for a in build_adapters()}
    # the real weclapp spec exposes well over 100 listable entities
    assert len(adapters) > 100
    assert {"salesOrder", "party", "article", "salesInvoice", "shipment"} <= set(adapters)
    so = adapters["salesOrder"].entity
    assert _field(so, "orderNumber").type == "string"
    cust = next(r for r in so.references if r.out_key() == "customerId")
    assert cust.reference == "party"
    assert any(c.out_key() == "orderItems" for c in so.collections)
    # the real spec exposes writes on salesOrder (get/post/put/delete)
    assert set(so.operations) >= {"list", "read", "create", "update", "delete"}
    assert _field(so, "orderNumber").writable is True
