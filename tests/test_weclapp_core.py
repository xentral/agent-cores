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
    # nested line-item fields stay non-writable in v1
    items = next(c for c in so.collections if c.out_key() == "orderItems")
    assert all(getattr(f, "writable", False) is False for f in items.fields)


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
    # writable business field carries no read-only access flag; collections do
    assert "access" not in props["orderNumber"]
    assert props["orderItems"]["access"] == "readOnly"


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
