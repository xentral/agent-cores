"""Return create can link the source order / delivery note and set a line reason.

v3 CreateReturnOrderData accepts salesOrder{id} / deliveryNote{id}, and each
lineItem accepts returnReason{id}. The facade maps documents.salesOrder /
documents.deliveryNote (create-only) and items[].reason accordingly.
"""

from __future__ import annotations

from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter


def _write(model, *, creating=True):
    return ReturnAdapter().map_write(model, creating=creating)


def test_links_sales_order_and_delivery_note_on_create():
    v3, rejected = _write(
        {
            "customer": {"id": "cus_20448"},
            "documents": {"salesOrder": {"id": "so_22919"}, "deliveryNote": "dn_1399"},
        }
    )
    assert rejected == set()
    assert v3["salesOrder"] == {"id": "22919"}
    assert v3["deliveryNote"] == {"id": "1399"}


def test_line_reason_maps_to_return_reason():
    v3, rejected = _write(
        {"items": [{"product": {"id": "prd_61983"}, "quantity": {"value": 2}, "reason": "rsn_5"}]}
    )
    assert rejected == set()
    li = v3["lineItems"][0]
    assert li["product"] == {"id": "61983"}
    assert li["quantity"] == 2
    assert li["returnReason"] == {"id": "5"}


def test_documents_rejected_on_update():
    _, rejected = _write({"documents": {"salesOrder": {"id": "so_1"}}}, creating=False)
    assert "documents" in rejected


def test_schema_marks_link_and_reason_creatable():
    fields = ReturnAdapter().fields()
    docs = fields["documents"]["properties"]
    assert docs["salesOrder"].get("creatable") and docs["deliveryNote"].get("creatable")
    reason = fields["items"]["node"]["properties"]["reason"]
    assert reason.get("creatable")
    assert reason.get("required")  # reason is mandatory per return policy


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
