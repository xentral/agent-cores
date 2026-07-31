"""Xentral V3 facade · storageLocation — Lagerplatz (docs/01-model.md §7.5).

Reads ``GET /v1/storageLocations`` (verified live — thin: id, designation,
warehouse). The target model's kind/pickingOrder/capacity need the scattered
v1/v2 fan-in (docs/03) — not yet composed, so those blocks are blue wishes;
``contents`` is answered by the StockLevel projection (filter[storageLocation]).

This is also WHERE WAREHOUSE WORK HAPPENS: the five stock actions (putaway,
stockRemoval, stockTransfer, inventoryCount, stockAdjustment) hang off the bin,
because that is what a warehouse acts on. They are the named entry points to the
booking orchestration in ``stock_movement`` — one implementation, addressed by
purpose instead of by a type discriminator plus a field combination.
"""

from __future__ import annotations

import json
from typing import Any

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref


class StorageLocationAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StorageLocation",
        label_en="Storage location",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.storageLocation",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),
    )
    v3_path = "/api/v1/storageLocations"
    include = ""
    preview_template = "{{name}}"
    query_aliases = {"name": "designation"}
    v1_paging = True
    sections = {"general": {"label": "General"}, "contents": {"label": "Contents"}}

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "block",
                        "Block",
                        wish="v1 storageLocations exposes no block/release state write.",
                    ),
                    self.step_cmd(
                        "release",
                        "Release",
                        wish="v1 storageLocations exposes no block/release state write.",
                    ),
                ],
            }
        ]

    # ---- warehouse actions (ADR-017) --------------------------------------
    # Named logistics operations, each with its own command schema, instead of
    # one stockMovement payload whose meaning follows from a discriminator plus a
    # field combination. The rules that used to live in prose ("quantity always
    # positive", "correction needs exactly ONE location", "quantity XOR
    # setQuantityTo") are schema here: putaway has no target, stockTransfer
    # requires one; inventoryCount takes an absolute quantity, stockAdjustment a
    # signed delta. An agent reads describe and knows — it cannot read a docstring.
    #
    # Vocabulary follows warehouse-management usage (SAP WM: Einlagerung /
    # Auslagerung / Umlagerung, MM: Inventur / Differenzbuchung). Deliberately NOT
    # goodsReceipt/goodsIssue: those are the MM document level and GoodsReceipt is
    # already an entity here — the same name for a bare stock booking would
    # collide with the Wareneingang document.
    _PRODUCT = {"type": "string", "label": "Product id (prd_…)"}
    _BATCH = {"type": "string", "label": "Batch / lot (batch-managed products only)"}
    _DRYRUN = {
        "type": "boolean",
        "label": "Validate and report what would be booked, without booking",
    }

    def actions(self):
        def cmd(props: dict, required: list[str]) -> dict:
            return {
                "type": "object",
                "required": required,
                "properties": {**props, "dryRun": self._DRYRUN},
            }

        return [
            self.action_def(
                "putaway",
                "Put away",
                description=(
                    "Einlagern: book stock ONTO this location. Irreversible — a "
                    "mistake is corrected by a counter-booking, not by an undo."
                ),
                command=cmd(
                    {
                        "product": self._PRODUCT,
                        "quantity": {"type": "number", "label": "Quantity to put away (> 0)"},
                        "batch": self._BATCH,
                        "reason": {"type": "string", "label": "Free-text note"},
                    },
                    ["product", "quantity"],
                ),
            ),
            self.action_def(
                "stockRemoval",
                "Stock removal",
                destructive=True,
                description=(
                    "Auslagern: book stock OFF this location. Reduces stock and "
                    "cannot be undone — only counter-booked."
                ),
                command=cmd(
                    {
                        "product": self._PRODUCT,
                        "quantity": {"type": "number", "label": "Quantity to remove (> 0)"},
                        "batch": self._BATCH,
                        "reason": {"type": "string", "label": "Free-text note"},
                    },
                    ["product", "quantity"],
                ),
            ),
            self.action_def(
                "stockTransfer",
                "Stock transfer",
                description=(
                    "Umlagern: move stock from THIS location to another one. Not "
                    "atomic upstream — on a partial failure the removal is "
                    "compensated and the outcome reported."
                ),
                command=cmd(
                    {
                        "product": self._PRODUCT,
                        "quantity": {"type": "number", "label": "Quantity to move (> 0)"},
                        "target": {
                            "type": "string",
                            "label": "Destination storage location id (loc_…)",
                        },
                        "batch": self._BATCH,
                    },
                    ["product", "quantity", "target"],
                ),
            ),
            self.action_def(
                "inventoryCount",
                "Inventory count",
                description=(
                    "Inventur: record the COUNTED quantity of a product on this "
                    "location; the difference to the book quantity is posted. The "
                    "only repeatable stock write — counting the same result twice "
                    "posts nothing the second time."
                ),
                command=cmd(
                    {
                        "product": self._PRODUCT,
                        "quantity": {
                            "type": "number",
                            "label": "Counted quantity — ABSOLUTE, not a delta (>= 0)",
                        },
                        "reason": {"type": "string", "label": "Count reference / note"},
                    },
                    ["product", "quantity"],
                ),
            ),
            self.action_def(
                "stockAdjustment",
                "Stock adjustment",
                destructive=True,
                description=(
                    "Bestandskorrektur: post a known difference against this "
                    "location. Requires a reason. Use inventoryCount when the "
                    "counted quantity is known instead of the difference."
                ),
                command=cmd(
                    {
                        "product": self._PRODUCT,
                        "quantity": {
                            "type": "number",
                            "label": "SIGNED delta: +3 books three on, -3 books three off",
                        },
                        "reason": {"type": "string", "label": "Why the stock was wrong"},
                        "batch": self._BATCH,
                    },
                    ["product", "quantity", "reason"],
                ),
            ),
            self.action_def(
                "printLabel", "Print label", wish="Storage-location labels have no public endpoint."
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=[
                    {"value": "active", "label": "Active"},
                    {"value": "blocked", "label": "Blocked"},
                ],
            ),
            "name": prop(
                "string",
                "Name",
                **RO,
                section="general",
                filterable=True,
                previewable=True,
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="general",
                previewable=True,
            ),
            "kind": prop(
                "select",
                "Kind",
                **RO,
                section="general",
                options=[
                    {"value": v, "label": v.capitalize()}
                    for v in ("picking", "bulk", "inbound", "returns", "quarantine")
                ],
            ),
            "pickingOrder": prop("integer", "Picking order", **RO, section="general"),
            "capacity": prop(
                "embedded",
                "Capacity",
                **RO,
                section="general",
                properties={
                    "maxWeight": prop(
                        "embedded",
                        "Max weight",
                        **RO,
                        properties={
                            "value": prop("decimal", "Value", **RO),
                            "unit": prop("string", "Unit", **RO),
                        },
                    ),
                    "note": prop("string", "Note", **RO),
                },
            ),
            "contents": prop(
                "collection",
                "Contents",
                **RO,
                section="contents",
                node={
                    "properties": {
                        "product": prop(
                            "reference", "Product", reference="Product", renderProperty="name", **RO
                        ),
                        "batch": prop(
                            "reference", "Batch", reference="Batch", renderProperty="number", **RO
                        ),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                        "reserved": prop(
                            "embedded",
                            "Reserved",
                            **RO,
                            properties={
                                "value": prop("decimal", "Value", **RO),
                                "unit": prop("string", "Unit", **RO),
                            },
                        ),
                    }
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        wh = r.get("warehouse")
        return {
            "object": "storageLocation",
            "status": ("blocked" if r.get("isRestrictedLocation") else "active"),
            "id": (f"loc_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
            "warehouse": ref(
                "wh_",
                wh.get("id") if isinstance(wh, dict) else wh,
                None,
                wh.get("name") if isinstance(wh, dict) else None,
                "warehouses",
            ),
            "kind": None,
            "pickingOrder": None,
            "capacity": {"maxWeight": None, "note": None},
            "contents": [],
            "createdAt": None,
            "updatedAt": None,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # v1 CRUD exists upstream but is not orchestrated here yet.
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}

    # ---- action dispatch --------------------------------------------------
    _STOCK_ACTIONS = (
        "putaway",
        "stockRemoval",
        "stockTransfer",
        "inventoryCount",
        "stockAdjustment",
    )

    async def action(  # noqa: ANN001
        self, *, action_key, handle, body, base_url, token, accept_language=None, client=None
    ):
        if action_key in self._STOCK_ACTIONS:
            return await self._book(
                action_key, handle, body, base_url, token, accept_language, client
            )
        return await super().action(
            action_key=action_key,
            handle=handle,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )

    def _movement(  # noqa: C901
        self, action_key: str, location: str, cmd: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Action command -> stockMovement model, validated in the ACTION's own
        vocabulary. The orchestrator validates again, but it speaks of to/from/
        setQuantityTo — fields this caller never sent, so its message would name
        something the caller cannot see."""
        problems: list[str] = []
        product = cmd.get("product")
        if isinstance(product, dict):
            product = product.get("id")
        if not product:
            problems.append("product is required")
        raw_qty = cmd.get("quantity")
        try:
            quantity = float(raw_qty)
        except (TypeError, ValueError):
            quantity = 0.0
            problems.append("quantity must be a number")
        reason = cmd.get("reason")
        batch = cmd.get("batch")

        if action_key == "stockAdjustment":
            if not reason:
                problems.append("reason is required — an adjustment without a cause is untraceable")
            if quantity == 0:
                problems.append("quantity must not be 0 (signed delta: + books on, - books off)")
        elif action_key == "inventoryCount":
            if quantity < 0:
                problems.append("quantity is the counted amount and cannot be negative")
        elif quantity <= 0:
            problems.append(
                "quantity must be > 0 — the direction comes from the action, not a sign"
            )

        target = cmd.get("target")
        if isinstance(target, dict):
            target = target.get("id")
        if action_key == "stockTransfer":
            if not target:
                problems.append("target (destination storage location) is required")
            elif str(target) == str(location):
                problems.append("target must differ from this location")
        if problems:
            return None, problems

        model: dict[str, Any] = {"product": product}
        if batch:
            model["batch"] = batch
        if action_key == "putaway":
            model |= {"type": "receipt", "quantity": {"value": quantity}, "to": location}
        elif action_key == "stockRemoval":
            model |= {"type": "issue", "quantity": {"value": quantity}, "from": location}
        elif action_key == "stockTransfer":
            model |= {
                "type": "transfer",
                "quantity": {"value": quantity},
                "from": location,
                "to": target,
            }
        elif action_key == "inventoryCount":
            model |= {"type": "correction", "setQuantityTo": quantity, "to": location}
            reason = reason or "Inventory count"
        elif action_key == "stockAdjustment":
            model |= {"type": "correction", "quantity": {"value": abs(quantity)}}
            model["to" if quantity > 0 else "from"] = location
        if reason:
            model["source"] = {"reason": reason}
        return model, []

    async def _level(  # noqa: ANN001
        self, product: str, location: str, base_url, token, accept_language, client
    ) -> dict[str, Any] | None:
        """The stock level of one product on one location, after the booking —
        the read-back every write owes its caller (ADR-018). ``None`` when the
        pair has no level yet (a removal down to zero drops the row upstream)."""
        from .stock_level import StockLevelAdapter
        from .stock_shared import numeric

        resp = await StockLevelAdapter().request(
            method="GET",
            handle=f"slv_{numeric(str(product))}_{numeric(str(location))}",
            query=[],
            body=None,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if resp.status_code >= 400:
            return None
        try:
            return json.loads(resp.content or b"{}").get("data")
        except ValueError:
            return None

    async def _book(  # noqa: ANN001
        self, action_key, handle, body, base_url, token, accept_language, client
    ) -> AdapterResponse:
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        if not isinstance(envelope, dict):
            envelope = {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if not ids:
            return self._json(
                422, {"title": f"{action_key} needs the storage location it acts on (ids[])"}
            )
        location = str(ids[0])
        cmd = envelope.get("command") or {}
        if not isinstance(cmd, dict):
            cmd = {}

        model, problems = self._movement(action_key, location, cmd)
        if problems:
            return self._json(
                422,
                {
                    "title": f"storageLocation.{action_key}: invalid command",
                    "problems": problems,
                },
            )

        from .stock_movement import StockMovementAdapter

        # dryRun rides in the command (the action envelope carries no query
        # string) and is translated onto the orchestrator's own switch.
        query = [("dryRun", "true")] if cmd.get("dryRun") in (True, "true", "1") else []
        booked = await StockMovementAdapter()._create_movement(  # noqa: SLF001
            query, json.dumps(model).encode(), base_url, token, accept_language, client
        )
        if booked.status_code >= 400 or query:
            return booked

        assert model is not None
        product = str(model["product"])
        out: dict[str, Any] = {
            "data": await self._level(product, location, base_url, token, accept_language, client),
            "result": {"action": action_key, "storageLocation": location},
        }
        if action_key == "stockTransfer":
            out["result"]["target"] = await self._level(
                product, str(model["to"]), base_url, token, accept_language, client
            )
        return self._json(200, out)
