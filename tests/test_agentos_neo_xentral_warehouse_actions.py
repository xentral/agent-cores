"""Warehouse actions on StorageLocation — named logistics operations.

Booking stock used to be one stockMovement payload whose meaning followed from a
`type` discriminator plus a field combination, with the rules living in a
docstring: quantity always positive, correction needs exactly ONE location,
quantity XOR setQuantityTo. An agent plans from `describe` and never sees a
docstring, so those rules were unreachable where they were needed.

They are schema now — putaway has no target, stockTransfer requires one;
inventoryCount takes an absolute quantity, stockAdjustment a signed delta. These
pin each action's mapping onto the booking orchestration, the validation in the
action's own vocabulary, and the read-back the action returns.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.storage_location import (
    StorageLocationAdapter,
)

_PRODUCT = {"id": "61985", "number": "E2E-0730-01", "name": "Zelt", "unit": "Stk"}
_LOCATIONS = {
    "1": {"id": "1", "designation": "Lagerplatz1", "warehouse": {"id": "9", "name": "Hauptlager"}},
    "4": {"id": "4", "designation": "Lagerplatz2", "warehouse": {"id": "9", "name": "Hauptlager"}},
}


class _Upstream:
    def __init__(self, *, on_shelf: float = 10.0):
        self.on_shelf = on_shelf
        self.item_calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.startswith("/api/v3/products/"):
            return httpx.Response(200, json={"data": _PRODUCT})
        if path == "/api/v1/storageLocations":
            row = _LOCATIONS.get(request.url.params.get("filter[0][value]"))
            return httpx.Response(200, json={"data": [row] if row else []})
        if path.startswith("/api/v1/products/") and path.endswith("/storageLocations"):
            # read-back projection: the product sits on location 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "1",
                            "amount": str(self.on_shelf),
                            "product": {"id": "61985"},
                            "storageLocation": {
                                "id": "1",
                                "name": "Lagerplatz1",
                                "warehouse": {"id": "9", "name": "Hauptlager"},
                            },
                        }
                    ],
                    "extra": {"page": {"number": 1, "size": 50}},
                },
            )
        if path.startswith("/api/v1/products/"):  # stock hydration
            return httpx.Response(200, json={"data": {"id": "61985", "totals": {}}})
        if path.startswith("/api/v2/warehouses/") and path.endswith("/items"):
            return httpx.Response(  # current quantity for the absolute count
                200,
                json={
                    "data": [
                        {"productId": "61985", "sku": "E2E-0730-01", "quantity": self.on_shelf}
                    ],
                    "extra": {"totalCount": 1, "page": {"number": 1, "size": 50}},
                },
            )
        if path.endswith("/items") and method in ("POST", "PATCH"):
            self.item_calls.append((method, path))
            return httpx.Response(201)
        raise AssertionError(f"unexpected call: {method} {path}")


def _run(up: _Upstream, action_key: str, command: dict[str, Any], location: str = "loc_1"):
    a = StorageLocationAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.action(
                action_key=action_key,
                handle=None,
                body=json.dumps({"ids": [location], "command": command}).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    return asyncio.run(go())


# ---- catalogue ---------------------------------------------------------


def test_catalogue_declares_the_five_operations_executably():
    actions = {a["key"]: a for a in StorageLocationAdapter().actions()}
    for key in ("putaway", "stockRemoval", "stockTransfer", "inventoryCount", "stockAdjustment"):
        assert key in actions, key
        assert "wish" not in actions[key], f"{key} must be executable, not a wish"
        assert actions[key]["command"]["type"] == "object"


def test_the_rules_that_were_prose_are_now_schema():
    actions = {a["key"]: a for a in StorageLocationAdapter().actions()}
    # only a transfer needs a destination
    assert "target" in actions["stockTransfer"]["command"]["required"]
    assert "target" not in actions["putaway"]["command"]["properties"]
    # only an adjustment demands a cause
    assert "reason" in actions["stockAdjustment"]["command"]["required"]
    assert "reason" not in actions["putaway"]["command"]["required"]
    # absolute vs signed is stated where the caller reads it
    assert "ABSOLUTE" in actions["inventoryCount"]["command"]["properties"]["quantity"]["label"]
    assert "SIGNED" in actions["stockAdjustment"]["command"]["properties"]["quantity"]["label"]


def test_removal_and_adjustment_are_flagged_destructive():
    actions = {a["key"]: a for a in StorageLocationAdapter().actions()}
    assert actions["stockRemoval"]["destructive"] is True
    assert actions["stockAdjustment"]["destructive"] is True
    assert actions["putaway"]["destructive"] is False


def test_request_count_wish_is_gone_because_it_is_implemented():
    """`requestCount` wished for exactly what inventoryCount now does — a wish
    that outlived its gap is a lie in the other direction."""
    keys = {a["key"] for a in StorageLocationAdapter().actions()}
    assert "requestCount" not in keys
    assert "inventoryCount" in keys


# ---- dispatch ----------------------------------------------------------


def test_putaway_books_onto_this_location():
    up = _Upstream()
    resp = _run(up, "putaway", {"product": "prd_61985", "quantity": 5})
    assert resp.status_code == 200
    assert up.item_calls == [("POST", "/api/v1/warehouses/9/storageLocations/1/items")]


def test_removal_books_off_this_location():
    up = _Upstream()
    resp = _run(up, "stockRemoval", {"product": "prd_61985", "quantity": 2})
    assert resp.status_code == 200
    assert up.item_calls == [("PATCH", "/api/v1/warehouses/9/storageLocations/1/items")]


def test_transfer_removes_here_then_puts_away_there():
    up = _Upstream()
    resp = _run(up, "stockTransfer", {"product": "prd_61985", "quantity": 2, "target": "loc_4"})
    assert resp.status_code == 200
    assert up.item_calls == [
        ("PATCH", "/api/v1/warehouses/9/storageLocations/1/items"),
        ("POST", "/api/v1/warehouses/9/storageLocations/4/items"),
    ]


def test_inventory_count_posts_only_the_difference():
    up = _Upstream(on_shelf=10.0)
    _run(up, "inventoryCount", {"product": "prd_61985", "quantity": 12})
    assert up.item_calls == [("POST", "/api/v1/warehouses/9/storageLocations/1/items")]


def test_counting_the_same_result_again_posts_nothing():
    """The repeatable write: count 10 when 10 are on the shelf → no upstream call."""
    up = _Upstream(on_shelf=10.0)
    resp = _run(up, "inventoryCount", {"product": "prd_61985", "quantity": 10})
    assert resp.status_code == 200
    assert up.item_calls == []


def test_adjustment_direction_follows_the_sign():
    up = _Upstream()
    _run(up, "stockAdjustment", {"product": "prd_61985", "quantity": -3, "reason": "Bruch"})
    assert up.item_calls == [("PATCH", "/api/v1/warehouses/9/storageLocations/1/items")]
    up = _Upstream()
    _run(up, "stockAdjustment", {"product": "prd_61985", "quantity": 3, "reason": "Fund"})
    assert up.item_calls == [("POST", "/api/v1/warehouses/9/storageLocations/1/items")]


# ---- guard rails -------------------------------------------------------


def test_validation_speaks_the_action_vocabulary_not_the_movement_one():
    up = _Upstream()
    resp = _run(up, "putaway", {"product": "prd_61985", "quantity": -1})
    assert resp.status_code == 422
    body = json.loads(resp.content)
    assert "putaway" in body["title"]
    problems = " ".join(body["problems"])
    assert "direction comes from the action" in problems
    assert "setQuantityTo" not in problems  # a field this caller never sent
    assert up.item_calls == []


def test_adjustment_without_a_cause_is_refused():
    up = _Upstream()
    resp = _run(up, "stockAdjustment", {"product": "prd_61985", "quantity": -3})
    assert resp.status_code == 422
    assert any("reason is required" in p for p in json.loads(resp.content)["problems"])
    assert up.item_calls == []


def test_transfer_onto_itself_is_refused():
    up = _Upstream()
    resp = _run(up, "stockTransfer", {"product": "prd_61985", "quantity": 1, "target": "loc_1"})
    assert resp.status_code == 422
    assert any("must differ" in p for p in json.loads(resp.content)["problems"])
    assert up.item_calls == []


def test_missing_target_is_refused_before_any_booking():
    up = _Upstream()
    resp = _run(up, "stockTransfer", {"product": "prd_61985", "quantity": 1})
    assert resp.status_code == 422
    assert up.item_calls == []


def test_action_without_a_location_is_refused():
    up = _Upstream()
    a = StorageLocationAdapter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await a.action(
                action_key="putaway",
                handle=None,
                body=json.dumps({"command": {"product": "prd_61985", "quantity": 1}}).encode(),
                base_url="https://unit.test",
                token="t",
                client=client,
            )

    assert asyncio.run(go()).status_code == 422


# ---- read-back and dry run --------------------------------------------


def test_the_action_answers_with_the_resulting_stock_level():
    """ADR-018: a write names its read-back. The caller gets the level it just
    changed, not an echo of what it sent."""
    up = _Upstream(on_shelf=15.0)
    resp = _run(up, "putaway", {"product": "prd_61985", "quantity": 5})
    body = json.loads(resp.content)
    assert body["data"]["object"] == "stockLevel"
    assert body["data"]["id"] == "slv_61985_1"
    assert body["data"]["quantity"]["value"] == 15.0
    assert body["result"]["action"] == "putaway"


def test_dry_run_reports_without_booking():
    up = _Upstream()
    resp = _run(up, "putaway", {"product": "prd_61985", "quantity": 5, "dryRun": True})
    assert resp.status_code == 200
    body = json.loads(resp.content)["data"]
    assert body["dryRun"] is True
    assert body["wouldBook"][0]["method"] == "POST"
    assert up.item_calls == []


class _EmptiedUpstream(_Upstream):
    """Upstream after a location was emptied: the product/location row is GONE,
    which is how Xentral represents zero (observed live 2026-07-31 — a transfer
    that emptied its source answered ``data: null``)."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/products/") and path.endswith("/storageLocations"):
            return httpx.Response(
                200, json={"data": [], "extra": {"page": {"number": 1, "size": 50}}}
            )
        return super().handler(request)


class _UnreadableUpstream(_Upstream):
    """The read-back itself fails — must NOT be reported as zero stock."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/products/") and path.endswith("/storageLocations"):
            return httpx.Response(500, json={"title": "upstream down"})
        return super().handler(request)


def test_emptying_a_location_reports_zero_not_null():
    up = _EmptiedUpstream()
    resp = _run(up, "stockRemoval", {"product": "prd_61985", "quantity": 10})
    assert resp.status_code == 200
    data = json.loads(resp.content)["data"]
    assert data is not None, "a successful booking must not answer with null"
    assert data["quantity"]["value"] == 0
    assert data["id"] == "slv_61985_1"


def test_a_zero_row_is_shaped_like_a_real_one():
    """Otherwise `warehouse` would be missing on exactly the records that report
    an empty bin — the caller cannot treat the two alike."""
    up = _EmptiedUpstream()
    data = json.loads(_run(up, "stockRemoval", {"product": "prd_61985", "quantity": 10}).content)[
        "data"
    ]
    assert data["storageLocation"]["name"] == "Lagerplatz1"
    assert data["warehouse"]["id"] == "wh_9"
    assert data["warehouse"]["name"] == "Hauptlager"


def test_an_unreadable_level_stays_null_and_is_never_served_as_zero():
    """The dangerous confusion: 'I could not read it' reported as 'there are
    zero' is a number a caller would act on."""
    up = _UnreadableUpstream()
    resp = _run(up, "putaway", {"product": "prd_61985", "quantity": 1})
    assert resp.status_code == 200  # the booking DID succeed
    assert json.loads(resp.content)["data"] is None


def test_unknown_action_still_falls_through_to_the_base():
    up = _Upstream()
    resp = _run(up, "printLabel", {})
    assert resp.status_code == 409  # declared wish, no upstream endpoint
    assert json.loads(resp.content)["wish"] is True
