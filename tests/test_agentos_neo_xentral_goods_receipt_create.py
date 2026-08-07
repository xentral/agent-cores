"""Receiving goods is an action on the ORDER, and it books stock as it writes.

`GoodsReceipt` was read-only with `post` declared as a wish, so the purchasing
chain had a manual UI step in the middle of it: you could order via the API and
never receive. v1 has had the endpoint all along — it is simply absent from the
OpenAPI spec, which is why the core recorded it as a gap.

It hangs off the parent document (`POST /api/v1/purchaseOrders/{id}/goodsReceipts`),
so it is modelled the way the core already models follow-up documents —
`SalesOrder.createSalesInvoice`, `DeliveryNote.createReturn` — as an action on the
source, not a create on the target. `GoodsReceipt` stays list/read, and `post`
stays a wish with a corrected reason: posting is not a transition, it is what
creation does.

The command speaks the model's vocabulary, not v1's. `items` (every document has
items), `orderItem` (as on DeliveryNote/SalesInvoice), and `putaways` — the core's
own word for booking stock onto a location (`StorageLocation.putaway`) rather than
v1's generic `stockMovements`. Upstream's nested `qualityControlAttributes` is
flattened to `batch` / `bestBefore` / `serialNumbers`, because the model has
batches and serial numbers, not a quality-control concept.

Verified live on mvp against a dedicated probe article: 5 of 7 booked → StockLevel
shows 5 on the location, and the order line's `fulfillment.received` goes 0 → 5.
"""

from __future__ import annotations

import json

import pytest
from xentral_entity_cores.agentos_neo_xentral.emulated.purchase_order import PurchaseOrderAdapter


@pytest.fixture
def adapter() -> PurchaseOrderAdapter:
    return PurchaseOrderAdapter()


def _command(adapter: PurchaseOrderAdapter) -> dict:
    actions = {a["key"]: a for a in adapter.metadata(None).get("actions") or []}
    return actions["createGoodsReceipt"]


def test_the_action_is_executable_and_declares_its_command(adapter):
    action = _command(adapter)
    assert not action.get("wish")
    assert action["destructive"] is True, "it moves stock irreversibly"
    assert sorted(action["command"]["required"]) == ["date", "items"]


def test_date_is_required_although_the_spec_calls_it_optional(adapter):
    """Measured: v1 answers 400 `Field \\`date\\` is required.` Not defaulted to today
    on purpose — the posting date of a stock booking is a decision, not a
    convenience, and a silently-wrong one is a GoBD problem."""
    assert "date" in _command(adapter)["command"]["required"]


def test_the_action_carries_no_dry_run():
    """Every StorageLocation action has one; this cannot. Declaring a dryRun the
    upstream ignores would be the worst possible lie about a stock booking."""
    props = _command(PurchaseOrderAdapter())["command"]["properties"]
    assert "dryRun" not in props
    item = props["items"]["items"]["properties"]
    assert "dryRun" not in item["putaways"]["items"]["properties"]


def test_the_command_speaks_the_model_vocabulary(adapter):
    """`items`/`orderItem`/`putaways`, not v1's `positions`/`purchaseOrderPosition`/
    `stockMovements` — one concept, one word, across the whole core."""
    props = _command(adapter)["command"]["properties"]
    assert set(props) == {"date", "items"}
    item = props["items"]["items"]["properties"]
    assert {"product", "quantity", "orderItem", "putaways"} == set(item)
    putaway = item["putaways"]["items"]["properties"]
    assert {"quantity", "warehouse", "storageLocation", "batch", "bestBefore", "serialNumbers"} == (
        set(putaway)
    )


class _Recorder(PurchaseOrderAdapter):
    """Captures the payload instead of posting it."""

    def __init__(self) -> None:
        super().__init__()
        self.payload: dict | None = None
        self.url: str | None = None


def _translate(command: dict) -> tuple[dict, str]:
    """Run the mapping and capture what would go on the wire."""
    import asyncio

    adapter = _Recorder()
    captured: dict = {}

    class _Client:
        async def post(self, url, json=None, headers=None):  # noqa: ANN001, A002
            captured["url"] = url
            captured["payload"] = json

            class _R:
                status_code = 201
                headers = {"Location": "https://x.test/api/v1/goodsReceipts/203"}

                @staticmethod
                def json():
                    return {}

            return _R()

    body = json.dumps({"ids": ["po_185"], "command": command}).encode()
    asyncio.run(
        adapter.action(
            action_key="createGoodsReceipt",
            handle="po_185",
            body=body,
            base_url="https://x.test",
            token="t",
            client=_Client(),
        )
    )
    return captured["payload"], captured["url"]


def test_the_model_command_is_translated_onto_the_v1_body():
    payload, url = _translate(
        {
            "date": "2026-08-07",
            "items": [
                {
                    "product": "prd_62006",
                    "quantity": 5,
                    "orderItem": "150",
                    "putaways": [
                        {
                            "quantity": 5,
                            "warehouse": "wh_20",
                            "storageLocation": "loc_163",
                            "batch": "L-42",
                            "bestBefore": "2027-01-31",
                            "serialNumbers": ["SN-1", "SN-2"],
                        }
                    ],
                }
            ],
        }
    )
    assert url.endswith("/api/v1/purchaseOrders/185/goodsReceipts")
    assert payload["date"] == "2026-08-07"
    position = payload["positions"][0]
    assert position["product"] == {"id": "62006"}
    assert position["quantity"] == 5
    assert position["purchaseOrderPosition"] == {"id": "150"}
    movement = position["stockMovements"][0]
    assert movement["warehouse"] == {"id": "20"} and movement["storageLocation"] == {"id": "163"}
    assert movement["qualityControlAttributes"] == {
        "batch": "L-42",
        "bestBeforeDate": "2027-01-31",
        "serialNumbers": [{"number": "SN-1"}, {"number": "SN-2"}],
    }


def test_a_receipt_without_putaways_books_no_location():
    """Receiving without assigning a location is allowed; the key must then be
    absent rather than an empty list, which upstream would reject."""
    payload, _ = _translate({"date": "2026-08-07", "items": [{"product": "prd_1", "quantity": 2}]})
    assert "stockMovements" not in payload["positions"][0]
    assert "purchaseOrderPosition" not in payload["positions"][0]


def test_the_created_receipt_id_is_reported():
    """v1 answers 201 with an empty body and a Location header. Without lifting the
    id out of it a workflow would have to go searching for what it just created."""
    import asyncio

    adapter = PurchaseOrderAdapter()

    class _Client:
        async def post(self, url, json=None, headers=None):  # noqa: ANN001, A002
            class _R:
                status_code = 201
                headers = {"Location": "https://x.test/api/v1/goodsReceipts/203"}

                @staticmethod
                def json():
                    return {}

            return _R()

    body = json.dumps(
        {
            "ids": ["po_185"],
            "command": {"date": "2026-08-07", "items": [{"product": "p", "quantity": 1}]},
        }
    ).encode()
    resp = asyncio.run(
        adapter.action(
            action_key="createGoodsReceipt",
            handle="po_185",
            body=body,
            base_url="https://x.test",
            token="t",
            client=_Client(),
        )
    )
    assert json.loads(resp.content)["data"] == {"object": "goodsReceipt", "id": "gr_203"}


@pytest.mark.parametrize(
    "command,expected",
    [
        ({"items": [{"product": "p", "quantity": 1}]}, "command.date"),
        ({"date": "2026-08-07"}, "command.items"),
        ({"date": "2026-08-07", "items": [{"quantity": 1}]}, "missing product"),
        ({"date": "2026-08-07", "items": [{"product": "p", "quantity": 0}]}, "must be > 0"),
    ],
)
def test_bad_commands_are_refused_before_they_reach_upstream(command, expected):
    """A stock booking is not the place to let a malformed payload through and read
    the upstream's error afterwards."""
    import asyncio

    adapter = PurchaseOrderAdapter()
    resp = asyncio.run(
        adapter.action(
            action_key="createGoodsReceipt",
            handle="po_185",
            body=json.dumps({"ids": ["po_185"], "command": command}).encode(),
            base_url="https://x.test",
            token="t",
            client=None,
        )
    )
    assert resp.status_code == 422
    assert expected in resp.content.decode()
