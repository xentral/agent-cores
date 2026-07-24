"""agentos_neo_postgres — adapter synthesis + SQL builder unit tests (no DB)."""

from __future__ import annotations

import json

from xentral_entity_cores.agentos_neo_postgres.emulated import build_adapters
from xentral_entity_cores.agentos_neo_postgres.emulated.base import (
    build_list_query,
    field_index,
    parse_query,
    snake,
)

ADAPTERS = {a.manifest.key: a for a in build_adapters()}


def test_all_model_entities_synthesize():
    assert len(ADAPTERS) == 22
    so = ADAPTERS["SalesOrder"]
    assert so._table == "neo_sales_order"
    assert so._prefix == "so_"
    assert set(so.manifest.operations) >= {"list", "read"}


def test_metadata_matches_contract_shape():
    meta = ADAPTERS["Customer"].metadata()
    assert meta["key"] == "Customer"
    assert meta["origin"] == "emulated"
    assert meta["emulation"]["adapter"] == "agentos_neo_postgres.Customer"
    props = meta["rootNode"]["properties"]
    assert isinstance(props, dict) and props
    # facade-specific stamps must not survive the snapshot
    flat = json.dumps(meta)
    assert '"verified"' not in flat
    assert '"priority"' not in flat


def test_snake():
    assert snake("SalesOrder") == "sales_order"
    assert snake("Tag") == "tag"


def _index(key: str = "SalesOrder"):
    return ADAPTERS[key]._index


def test_filter_equals_and_paging():
    parsed = parse_query(
        [
            ("filter[0][key]", "number"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "SO-1"),
            ("page[number]", "2"),
            ("page[size]", "10"),
        ]
    )
    sql, args = build_list_query("neo_sales_order", _index(), parsed)
    assert "data #>> '{number}' = $1" in sql
    assert "LIMIT 10 OFFSET 10" in sql
    assert args == ["SO-1"]


def test_filter_reference_targets_id():
    parsed = parse_query(
        [
            ("filter[0][key]", "customer"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", "cus_7"),
        ]
    )
    sql, args = build_list_query("neo_sales_order", _index(), parsed)
    assert "data #>> '{customer,id}' = $1" in sql
    assert args == ["cus_7"]


def test_filter_numeric_casts():
    index = _index("Product")
    numeric = next(
        (p for p, s in index.items() if s.get("type") in ("number", "decimal", "integer")),
        None,
    )
    if numeric is None:  # model has no numeric field on Product — nothing to test
        return
    parsed = parse_query(
        [
            ("filter[0][key]", numeric),
            ("filter[0][op]", "greaterThan"),
            ("filter[0][value]", "5"),
        ]
    )
    sql, args = build_list_query("neo_product", index, parsed)
    assert "::numeric" in sql
    assert args == [5.0]


def test_unknown_field_and_injection_are_rejected():
    for bad in ("nope", "id; DROP TABLE x", "a' OR '1'='1"):
        parsed = parse_query(
            [("filter[0][key]", bad), ("filter[0][op]", "equals"), ("filter[0][value]", "x")]
        )
        try:
            build_list_query("neo_sales_order", _index(), parsed)
        except ValueError as exc:
            assert "unknown filter field" in str(exc)
        else:
            raise AssertionError(f"{bad!r} was not rejected")


def test_sort_with_tiebreak_and_search():
    parsed = parse_query([("sort", "-number"), ("searchTerm", "acme")])
    sql, args = build_list_query("neo_sales_order", _index(), parsed)
    assert "DESC NULLS LAST, id" in sql
    assert "ILIKE" in sql
    assert "acme" in args


def test_default_order_is_deterministic():
    sql, _ = build_list_query("neo_sales_order", _index(), parse_query([]))
    assert "ORDER BY created_at DESC, id" in sql


def test_field_index_walks_nested():
    index = field_index(
        {
            "a": {"type": "embedded", "properties": {"b": {"type": "string"}}},
            "c": {"type": "collection", "node": {"properties": {"d": {"type": "number"}}}},
        }
    )
    assert set(index) == {"a", "a.b", "c", "c.d"}


def test_write_validation_rejects_readonly_and_unknown():
    so = ADAPTERS["SalesOrder"]
    payload, err = so._validate_write(json.dumps({"id": "x"}).encode(), creating=True)
    assert payload is None and err is not None
    body = json.loads(err.content)
    assert err.status_code == 409 and "id" in body["fields"]

    payload, err = so._validate_write(
        json.dumps({"definitely_not_a_field": 1}).encode(), creating=False
    )
    assert err is not None and err.status_code == 409


def test_write_validation_accepts_writable():
    so = ADAPTERS["SalesOrder"]
    writable = so._writable_paths(creating=True)
    assert writable, "SalesOrder should have creatable fields"
    field = sorted(writable)[0]
    payload, err = so._validate_write(json.dumps({field: "x"}).encode(), creating=True)
    if err is not None:  # only acceptable failure: required fields missing
        assert err.status_code == 422
    else:
        assert payload == {field: "x"}
