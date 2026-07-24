"""Unit tests for the core-agnostic entity field summariser.

Covers the two guarantees the ``xentral_entities`` ``describe`` output relies on:
per-field write/query flags (``summarize_props``) and the flat query contract
(``query_contract``). Both read only the schema flag vocabulary shared by every
core, so a synthetic schema exercises the generic behaviour and a real
``agentos_neo`` schema pins the dotted-path contract end to end.
"""

from __future__ import annotations

from mcp_server.tools._entity_fields import query_contract, summarize_props


def _synthetic_props() -> dict:
    """A schema fragment exercising every shape the summariser must handle:
    a filterable/sortable/searchable scalar, a select with options, a read-only
    field, a create-only field, and a collection whose nested leaf is the only
    filterable key (so the flat contract must emit the dotted path)."""
    return {
        "name": {
            "type": "string",
            "label": "Name",
            "filterable": True,
            "sortable": True,
            "searchable": True,
            "creatable": True,
            "updatable": True,
        },
        "status": {
            "type": "select",
            "label": "Status",
            "access": "readOnly",
            "options": [
                {"value": "active", "label": "Active"},
                {"value": "archived", "label": "Archived"},
            ],
        },
        "number": {
            "type": "string",
            "label": "Number",
            "creatable": True,  # settable on create, not on update
            "filterable": True,
        },
        "addresses": {
            "type": "collection",
            "label": "Addresses",
            "creatable": True,
            "updatable": True,
            "node": {
                "properties": {
                    "id": {"type": "string", "access": "readOnly"},
                    "city": {
                        "type": "string",
                        "label": "City",
                        "filterable": True,
                        "sortable": True,
                        "searchable": True,
                        "creatable": True,
                        "updatable": True,
                    },
                }
            },
        },
    }


def test_summarize_props_surfaces_write_and_query_flags():
    fields = {f["name"]: f for f in summarize_props(_synthetic_props(), depth=2)}

    # writable scalar: creatable + updatable, no access key
    assert fields["name"]["creatable"] is True
    assert fields["name"]["updatable"] is True
    assert "access" not in fields["name"]
    # query flags surfaced only when true
    assert fields["name"]["filterable"] is True
    assert fields["name"]["sortable"] is True
    assert fields["name"]["searchable"] is True

    # read-only select: access preserved, options flattened to values, no write flags
    assert fields["status"]["access"] == "readOnly"
    assert fields["status"]["options"] == ["active", "archived"]
    assert "creatable" not in fields["status"]
    assert "filterable" not in fields["status"]

    # create-only field: creatable present, updatable absent
    assert fields["number"]["creatable"] is True
    assert "updatable" not in fields["number"]

    # nested leaf flags survive one expansion level
    city = {f["name"]: f for f in fields["addresses"]["fields"]}["city"]
    assert city["filterable"] is True
    assert city["creatable"] is True


def test_query_contract_flattens_dotted_keys_and_is_stable():
    qc = query_contract(_synthetic_props())

    # stable three-key shape, always present
    assert set(qc) == {"filterable", "sortable", "searchable"}

    filter_keys = [e["key"] for e in qc["filterable"]]
    # nested leaf surfaces under its dotted path, scalars under their bare name
    assert filter_keys == ["addresses.city", "name", "number"]  # sorted
    assert qc["sortable"] == ["addresses.city", "name"]
    assert qc["searchable"] == ["addresses.city", "name"]

    # filterable entries carry the value shape
    by_key = {e["key"]: e for e in qc["filterable"]}
    assert by_key["name"]["type"] == "string"
    assert by_key["addresses.city"]["type"] == "string"


def test_query_contract_carries_select_options_and_references():
    props = {
        "taxation": {
            "type": "select",
            "filterable": True,
            "options": [{"value": "standard", "label": "Standard"}],
        },
        "channel": {
            "type": "reference",
            "filterable": True,
            "reference": "Channel",
        },
    }
    by_key = {e["key"]: e for e in query_contract(props)["filterable"]}
    assert by_key["taxation"]["options"] == ["standard"]
    assert by_key["channel"]["references"] == "Channel"


def test_agentos_neo_customer_contract_end_to_end():
    """Regression against the real active-core schema: the dotted address filter
    is exposed, a writable-but-not-queryable field (vatId) stays out of the
    filter contract, and read-only finance fields are not writable."""
    from agentos_neo.emulated.customer import CustomerAdapter

    props = CustomerAdapter().metadata("en")["rootNode"]["properties"]
    qc = query_contract(props)
    filter_keys = {e["key"] for e in qc["filterable"]}

    assert "addresses.city" in filter_keys
    assert "name" in filter_keys and "number" in filter_keys
    # vatId is writable only — must not appear as a query key
    assert "vatId" not in filter_keys
    assert "vatId" not in set(qc["sortable"]) | set(qc["searchable"])

    fields = {f["name"]: f for f in summarize_props(props, depth=2)}
    # vatId is writable (create + update), no access marker
    assert fields["vatId"].get("creatable") is True
    assert "access" not in fields["vatId"]
    # finance block is read-only
    assert fields["finance"].get("access") == "readOnly"
