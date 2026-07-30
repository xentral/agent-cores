"""Return create can link the source order / delivery note and set a line reason.

v3 CreateReturnOrderData accepts salesOrder{id} / deliveryNote{id}, and each
lineItem accepts returnReason{id}. The facade maps documents.salesOrder /
documents.deliveryNote (create-only) and items[].reason accordingly.
"""

from __future__ import annotations

import asyncio
import json

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


class _Resp:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload or {
            "data": {"id": 500, "documentNumber": "301295-R", "lineItems": []}
        }

    def json(self):
        return self._payload


class _Client:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls.append((url, json))
        return _Resp()


def _cfdn(command):
    a = ReturnAdapter()
    client = _Client()
    resp = asyncio.run(
        a.action(
            action_key="createFromDeliveryNote",
            handle=None,
            body=json.dumps({"command": command}).encode(),
            base_url="https://x",
            token="t",
            client=client,
        )
    )
    return resp, client


def test_create_from_delivery_note_builds_v3_body():
    resp, client = _cfdn(
        {
            "deliveryNote": "dn_1399",
            "lineItems": [
                {
                    "deliveryNoteItem": "itm_2994",
                    "quantity": 2,
                    "reason": "rsn_18",
                    "description": "unhappy",
                }
            ],
        }
    )
    assert resp.status_code == 201
    url, body = client.calls[0]
    assert url.endswith("/api/v3/returnOrders/actions/createFromDeliveryNote")
    assert body["deliveryNote"] == {"id": "1399"}
    assert body["lineItems"][0] == {
        "id": "2994",
        "quantity": 2.0,
        "returnReason": {"id": "18"},
        "description": "unhappy",
    }


def test_create_from_delivery_note_requires_reason_per_line():
    resp, client = _cfdn(
        {"deliveryNote": "dn_1399", "lineItems": [{"deliveryNoteItem": "itm_2994", "quantity": 2}]}
    )
    assert resp.status_code == 422
    assert client.calls == []  # nothing posted


def test_create_from_delivery_note_needs_delivery_note_and_lines():
    resp, client = _cfdn({"lineItems": []})
    assert resp.status_code == 422
    assert client.calls == []


def test_action_advertises_create_from_delivery_note():
    a = ReturnAdapter()
    by_key = {x["key"]: x for x in a.actions()}
    assert "createFromDeliveryNote" in by_key
    cmd = by_key["createFromDeliveryNote"]["command"]["properties"]
    assert "deliveryNote" in cmd and "lineItems" in cmd


def test_release_freigeben_maps_to_v3_release():
    # freigeben from draft — uniform 'release' op across all documents.
    assert ReturnAdapter().action_map["release"] == ("PATCH", "release")


def test_release_is_a_document_status_step():
    groups = {g["key"]: g for g in ReturnAdapter().steps()}
    keys = [c["key"] for c in groups["documentStatus"]["commands"]]
    assert "release" in keys
    assert {"settle", "cancel"} <= set(keys)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
