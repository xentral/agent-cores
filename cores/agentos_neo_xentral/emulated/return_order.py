"""Xentral V3 facade · return — Retoure (docs/01-model.md §4.6).

Reads Xentral v3 ``/api/v3/returnOrders``. The new status chain
(requested→received→checked→settled) maps from the upstream ``progress`` field
(``status`` only gates cancelled). Per-item condition/action are not in the v3
payload yet (blue wishes). Per ADR-014 only upstream-writable fields are
creatable/updatable; the rest are blue wishes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import (
    _TIMEOUT,
    FacadeAdapterBase,
    RO,
    line_qty,
    map_tags,
    prop,
    ref,
    status_map,
    tags_prop,
    tags_to_v3,
)

_PROGRESS = {
    "announced": "requested",
    "received": "received",
    "checked": "checked",
    # ReturnOrderProgress upstream is announced|received|checked|done — `done` was
    # missing, and it is the most common value on mvp (35 of 82).
    "done": "settled",
    "booked": "settled",
    "settled": "settled",
}
_STATUS_OPTIONS = [
    {"value": v, "label": v.capitalize()}
    for v in ("requested", "received", "checked", "settled", "cancelled")
]
_CU = {"creatable": True, "updatable": True}


def _address_props() -> dict[str, Any]:
    s = lambda label: prop("string", label, **_CU)  # noqa: E731
    return {
        "name": s("Name"),
        "street": s("Street"),
        "zip": s("Zip"),
        "city": s("City"),
        "country": s("Country"),
        "email": s("Email"),
        "phone": s("Phone"),
        "vatId": s("VAT id"),
    }


class ReturnAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Return",
        label_en="Return",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.return",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/v3/returnOrders"
    renders_pdf = True
    include = "lineItems,lineItems.product,address,tags"
    preview_template = "{{number}}"
    query_aliases = {
        "documents.salesOrder": "salesOrder.id",
        "items.product": "lineItems.product.id",
        "number": "documentNumber",
        "dates.requested": "documentDate",
        "customer": "address.id",
        "project": "project.id",
        "references.customerOrderNumber": "customerOrderNumber",
        "tags": "tags",
    }
    filter_value_maps = {
        "status": {"requested": "released", "checked": "completed", "settled": "completed"}
    }
    sections = {
        "general": {"label": "General"},
        "references": {"label": "References"},
        "address": {"label": "Address"},
        "items": {"label": "Items"},
        "flow": {"label": "Document flow"},
    }

    action_map = {
        # Release / freigeben from draft (v3 release) — uniform across documents.
        "release": ("PATCH", "release"),
        "settle": ("PATCH", "complete"),
        "cancel": ("PATCH", "cancel"),
        # Settle the return by issuing a credit note (the return's resolution).
        # POST /api/v1/returns/{id}/actions/createCreditNote; isApproved/isPaid
        # are required upstream — default to approved & not-yet-paid, overridable
        # via the command. Returns the created credit note under `result`.
        "createCreditNote": {
            "method": "POST",
            "path": "/api/v1/returns/{id}/actions/createCreditNote",
            "body": {"isApproved": True, "isPaid": False},
        },
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Document status",
                "commands": [
                    # Release / freigeben from draft (v3 release) — uniform op.
                    self.step_cmd("release", "Release"),
                    self.step_cmd(
                        "receive",
                        "Receive",
                        wish="Receiving has no upstream endpoint — v3 returnOrders offers complete/cancel only.",
                    ),
                    self.step_cmd(
                        "check", "Check", wish="The check step has no upstream endpoint."
                    ),
                    self.step_cmd("settle", "Settle"),
                    self.step_cmd("cancel", "Cancel"),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "createFromDeliveryNote",
                "Create return from delivery note",
                description=(
                    "Best-practice one-step return: create a return directly from a "
                    "delivery note (the goods that were shipped). Provide the delivery "
                    "note id and the lines to return — each with the delivery-note line "
                    "item id, the quantity, and a REQUIRED ReturnReason id (list them "
                    "via the ReturnReason entity)."
                ),
                command={
                    "type": "object",
                    "required": ["deliveryNote", "lineItems"],
                    "properties": {
                        "deliveryNote": {"type": "string", "label": "Delivery note id"},
                        "lineItems": {
                            "type": "array",
                            "label": "Lines to return",
                            "items": {
                                "type": "object",
                                "required": ["deliveryNoteItem", "quantity", "reason"],
                                "properties": {
                                    "deliveryNoteItem": {
                                        "type": "string",
                                        "label": "Delivery-note line item id",
                                    },
                                    "quantity": {"type": "number", "label": "Quantity to return"},
                                    "reason": {
                                        "type": "string",
                                        "label": "ReturnReason id (required)",
                                    },
                                    "description": {"type": "string", "label": "Note"},
                                },
                            },
                        },
                    },
                },
            ),
            self.action_def(
                "sendReturnLabel",
                "Send return label",
                wish="Return labels run through the beta carrier label API — not public.",
            ),
            self.action_def(
                "createCreditNote",
                "Create credit note",
                destructive=True,
                description=(
                    "Settle the return by issuing a credit note (Gutschrift) for the "
                    "returned goods — the return's financial resolution. Optional "
                    "command {isApproved, isPaid} (both default: approved, unpaid). "
                    "A still-unreleased (draft) return is released first: a credit note "
                    "issued from a draft leaves the return without a document number and "
                    "it can never be released afterwards. The created credit note is "
                    "linked on the return under `resolution.creditNote`."
                ),
                command={
                    "type": "object",
                    "properties": {
                        "isApproved": {"type": "boolean", "label": "Approve the credit note"},
                        "isPaid": {"type": "boolean", "label": "Mark the refund as paid"},
                    },
                },
            ),
            self.action_def(
                "createReplacementOrder",
                "Create replacement order",
                wish="No replacement-order endpoint upstream.",
            ),
            self.action_def(
                "restock",
                "Restock",
                wish="Restocking runs through goods receipt — no direct endpoint.",
            ),
            self.action_def(
                "downloadPdf",
                "Download PDF",
                description=(
                    "Fetch the rendered document as a PDF file. Upstream serves the "
                    "archived copy when one exists (written on send and on write "
                    "protection) and renders fresh otherwise. Returns the bytes as "
                    "result.file (base64) — hand it to a file store rather than "
                    "reading it."
                ),
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "number": prop(
                "string",
                "Number",
                **RO,
                section="general",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
            ),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=_STATUS_OPTIONS,
                filterable=True,
                previewable=True,
            ),
            "customer": prop(
                "reference",
                "Customer",
                reference="Customer",
                renderProperty="name",
                section="general",
                creatable=True,
                filterable=True,
                previewable=True,
            ),
            "project": prop(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                section="general",
                **_CU,
            ),
            "references": prop(
                "embedded",
                "References",
                section="references",
                properties={
                    "rmaNumber": prop("string", "RMA number", description="Not filterable — the upstream list endpoint rejects it (verified on mvp)."),
                    "customerOrderNumber": prop("string", "Customer order number", filterable=True),
                },
            ),
            "dates": prop(
                "embedded",
                "Dates",
                section="general",
                properties={
                    "requested": prop("date", "Requested", **_CU, filterable=True, sortable=True),
                    "received": prop("date", "Received", **RO),
                    "settled": prop("date", "Settled", **RO),
                },
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                section="general",
            ),
            "billingAddress": prop(
                "embedded", "Address", section="address", properties=_address_props()
            ),
            "items": prop(
                "collection",
                "Items",
                section="items",
                node={
                    "properties": {
                        "object": prop("string", "Object", **RO),
                        "id": prop("string", "Item id", **RO),
                        "position": prop("integer", "Position"),
                        "deliveryNoteItem": prop(
                            "reference",
                            "Delivery note item",
                            reference="DeliveryNote",
                            renderProperty="number",
                            **RO,
                        ),
                        "product": prop(
                            "reference",
                            "Product",
                            reference="Product",
                            renderProperty="name",
                            creatable=True,
                            filterable=True,
                        ),
                        "quantity": prop(
                            "embedded",
                            "Quantity",
                            creatable=True,
                            properties={
                                "value": prop("decimal", "Value"),
                                "unit": prop("string", "Unit"),
                            },
                        ),
                        "reason": prop(
                            "reference",
                            "Reason",
                            reference="ReturnReason",
                            renderProperty="name",
                            creatable=True,
                            required=True,
                        ),
                        "condition": prop("select", "Condition"),
                        "action": prop("select", "Action"),
                        "receivedQuantity": prop("decimal", "Received quantity", **RO),
                        "creditedQuantity": prop("decimal", "Credited quantity", **RO),
                    }
                },
            ),
            "resolution": prop(
                "embedded",
                "Resolution",
                **RO,
                section="flow",
                properties={
                    "creditNote": prop(
                        "reference",
                        "Credit note",
                        reference="CreditNote",
                        renderProperty="number",
                        **RO,
                    ),
                    "replacementOrder": prop(
                        "reference",
                        "Replacement order",
                        reference="SalesOrder",
                        renderProperty="number",
                        **RO,
                    ),
                },
            ),
            "note": prop("string", "Note", section="general", **_CU),
            "documents": prop(
                "embedded",
                "Documents",
                section="flow",
                properties={
                    # Create-only: link the return to its source order / delivery note
                    # (v3 salesOrder{id} / deliveryNote{id}). On read they show the
                    # linked documents.
                    "salesOrder": prop(
                        "reference",
                        "Sales order",
                        reference="SalesOrder",
                        renderProperty="number",
                        creatable=True,
                        filterable=True,
                        description=(
                            "The sales order this return refers to. Filterable — the "
                            "way to ask which returns an order produced."
                        ),
                    ),
                    "deliveryNote": prop(
                        "reference",
                        "Delivery note",
                        reference="DeliveryNote",
                        renderProperty="number",
                        creatable=True,
                    ),
                },
            ),
            "tags": tags_prop(writable=True),
            "customFields": prop("embedded", "Custom fields", section="general", properties={}),
            "createdAt": prop(
                "datetime",
                "Created at",
                **RO,
                sortable=True,
                filterable=True,
                description="When the record was created. Filterable.",
            ),
            "updatedAt": prop(
                "datetime",
                "Updated at",
                **RO,
                sortable=True,
                filterable=True,
                description=(
                    "When the record last changed. Filterable — this is the key for "
                    "an incremental sync: ask for what changed since the last run "
                    "instead of paging the whole collection."
                ),
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        def addr(a: dict[str, Any] | None, vat: Any = None) -> dict[str, Any] | None:
            if not isinstance(a, dict):
                return None
            return {
                "name": a.get("name"),
                "street": a.get("street"),
                "zip": a.get("zipCode"),
                "city": a.get("city"),
                "country": a.get("country"),
                "email": a.get("email"),
                "phone": a.get("phone"),
                "vatId": vat,
            }

        status = (
            "cancelled"
            if r.get("status") == "cancelled"
            else status_map(_PROGRESS, r.get("progress"), "requested")
        )
        items = []
        for li in r.get("lineItems") or []:
            if not isinstance(li, dict) or li.get("type") == "text":
                continue
            p = li.get("product") or {}
            dnli = li.get("deliveryNoteLineItem") or {}
            reason = li.get("returnReason") or {}
            items.append(
                {
                    "object": "returnItem",
                    "id": str(li.get("id")) if li.get("id") else None,
                    "position": li.get("order"),
                    "deliveryNoteItem": ref("itm_", dnli.get("id"), None, None, "deliveryNotes")
                    if dnli.get("id")
                    else None,
                    "product": ref(
                        "prd_", p.get("id"), p.get("number"), li.get("name"), "products"
                    ),
                    "quantity": {"value": li.get("quantity"), "unit": li.get("unit") or "piece"},
                    "reason": ref(
                        "rsn_", reason.get("id"), None, reason.get("name"), "returnReasons"
                    )
                    if reason.get("id")
                    else None,
                    "condition": None,
                    "action": None,
                    "receivedQuantity": li.get("receivedQuantity"),
                    "creditedQuantity": li.get("reimbursementQuantity"),
                }
            )
        so, dn = r.get("salesOrder"), r.get("deliveryNote")
        cn, rso = r.get("creditNote"), r.get("replacementSalesOrder")
        return {
            "object": "return",
            "id": (f"ret_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("documentNumber"),
            "status": status,
            "customer": ref(
                "cus_",
                (r.get("address") or {}).get("id"),
                r.get("customerNumber"),
                (r.get("documentAddress") or {}).get("name"),
                "customers",
            ),
            "project": ref(
                "prj_",
                (r.get("project") or {}).get("id"),
                None,
                (r.get("project") or {}).get("name"),
                "projects",
            ),
            "references": {"rmaNumber": None, "customerOrderNumber": r.get("customerOrderNumber")},
            "dates": {"requested": r.get("documentDate"), "received": None, "settled": None},
            "warehouse": ref(
                "wh_",
                (r.get("preferredWarehouse") or {}).get("id"),
                None,
                (r.get("preferredWarehouse") or {}).get("name"),
                "warehouses",
            ),
            "billingAddress": addr(r.get("documentAddress"), r.get("vatId")),
            "items": items,
            "resolution": {
                "creditNote": ref(
                    "cn_", cn.get("id") if isinstance(cn, dict) else cn, None, None, "creditNotes"
                ),
                "replacementOrder": ref(
                    "so_",
                    rso.get("id") if isinstance(rso, dict) else rso,
                    None,
                    None,
                    "salesOrders",
                ),
            },
            "note": r.get("internalComment"),
            "documents": {
                "salesOrder": ref(
                    "so_", so.get("id") if isinstance(so, dict) else so, None, None, "salesOrders"
                ),
                "deliveryNote": ref(
                    "dn_", dn.get("id") if isinstance(dn, dict) else dn, None, None, "deliveryNotes"
                ),
            },
            "tags": map_tags(r.get("tags")),
            "customFields": r.get("customFields") or {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    _WRITABLE = {
        "customer",
        "project",
        "note",
        "billingAddress",
        "items",
        "dates",
        "tags",
        "documents",
    }
    # `number` is deliberately NOT ignored: a document number always comes from the
    # configured number range, so a caller supplying one must be told it was refused
    # rather than get a 201 and a different number. Upstream would accept it on three
    # of these types (salesOrder / invoice / creditNote, verified on mvp) — declining
    # it everywhere is a product decision, recorded as such in priorities.json.
    _IGNORE = {
        "object",
        "id",
        "status",
        "warehouse",
        "resolution",
        "createdAt",
        "updatedAt",
    }

    @staticmethod
    def _ref_id(v: Any) -> dict[str, Any] | None:
        if isinstance(v, dict):
            ident = v.get("id") or v.get("number")
            return (
                {"id": str(ident).split("_", 1)[1] if "_" in str(ident) else str(ident)}
                if ident
                else None
            )
        return (
            {"id": str(v).split("_", 1)[1] if "_" in str(v) else str(v)}
            if v not in (None, "")
            else None
        )

    @staticmethod
    def _addr_to_v3(a: dict[str, Any] | None) -> dict[str, Any]:
        a = a or {}
        return {
            k: a.get(src)
            for k, src in (
                ("name", "name"),
                ("street", "street"),
                ("zipCode", "zip"),
                ("city", "city"),
                ("country", "country"),
                ("email", "email"),
                ("phone", "phone"),
            )
            if a.get(src) is not None
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        v3: dict[str, Any] = {}
        rejected: set[str] = set()
        if "project" in model:
            v3["project"] = self._ref_id(model["project"])
        if "note" in model:
            v3["internalComment"] = model["note"]
        if "billingAddress" in model:
            v3["documentAddress"] = self._addr_to_v3(model["billingAddress"])
            if (model["billingAddress"] or {}).get("vatId"):
                v3["vatId"] = model["billingAddress"]["vatId"]
        if "dates" in model and (model["dates"] or {}).get("requested"):
            v3["documentDate"] = model["dates"]["requested"]
        if "customer" in model:
            if creating:
                v3["address"] = self._ref_id(model["customer"])
            else:
                rejected.add("customer")
        if "items" in model:
            if creating:
                v3["lineItems"] = [
                    self._item_to_v3(i) for i in model["items"] if isinstance(i, dict)
                ]
            else:
                rejected.add("items")
        if "documents" in model:
            # Link the return to its source order / delivery note (create-only).
            if creating:
                docs = model["documents"] or {}
                so = self._ref_id(docs.get("salesOrder")) if isinstance(docs, dict) else None
                if so is not None:
                    v3["salesOrder"] = so
                dn = self._ref_id(docs.get("deliveryNote")) if isinstance(docs, dict) else None
                if dn is not None:
                    v3["deliveryNote"] = dn
            else:
                rejected.add("documents")
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        for k in model:
            if k in self._WRITABLE or k in self._IGNORE:
                continue
            rejected.add(k)
        return v3, rejected

    @staticmethod
    def _item_to_v3(i: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        prod = i.get("product")
        if prod is not None:
            pid = prod.get("id") if isinstance(prod, dict) else prod
            out["product"] = {"id": str(pid).split("_", 1)[1] if "_" in str(pid) else str(pid)}
        qty_val = line_qty(i)
        if qty_val is not None:
            out["quantity"] = qty_val
        if i.get("discountPercent") is not None:
            out["discount"] = i["discountPercent"]
        reason = i.get("reason")
        if reason is not None:
            rid = reason.get("id") if isinstance(reason, dict) else reason
            if rid not in (None, ""):
                out["returnReason"] = {
                    "id": str(rid).split("_", 1)[1] if "_" in str(rid) else str(rid)
                }
        return out

    # ---- best-practice one-shot: create a return from a delivery note ----
    async def action(  # noqa: ANN001
        self, *, action_key, handle, body, base_url, token, accept_language=None, client=None
    ):
        if action_key == "createFromDeliveryNote":
            return await self._create_from_delivery_note(
                body, base_url, token, accept_language, client
            )
        if action_key == "createCreditNote":
            blocked = await self._release_before_credit_note(
                handle=handle,
                body=body,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
            if blocked is not None:
                return blocked
        return await super().action(
            action_key=action_key,
            handle=handle,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )

    async def _release_before_credit_note(  # noqa: ANN001
        self, *, handle, body, base_url, token, accept_language, client
    ):
        """Never issue a credit note from an unreleased draft return.

        Upstream happily accepts createCreditNote on a draft: it mints a real,
        numbered credit note while the return itself keeps no document number
        and can no longer be released afterwards ("Only a draft ReturnOrder can
        be released") — the source strands in draft forever. `status` cannot
        tell the two apart (it reads `requested` both before and after a
        release), so the document number is the only usable marker.

        Releases the return first and returns a 409 when that fails; returns
        None when the caller may proceed.
        """
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        ids = envelope.get("ids") or ([handle] if handle else [])
        if not ids:
            return None  # the base action reports the missing target itself
        up_id = str(ids[0]).split("_", 1)[1] if "_" in str(ids[0]) else str(ids[0])

        status, payload = await self._get(
            base_url,
            token,
            handle=up_id,
            query=[],
            accept_language=accept_language,
            client=client,
        )
        if status >= 400:
            return None  # let the action itself surface the upstream error
        rec = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rec, dict) or rec.get("documentNumber") not in (None, ""):
            return None  # already released — nothing to do

        url = f"{base_url.rstrip('/')}{self.v3_path}/{up_id}/actions/release"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001, ANN202
            return await c.request("PATCH", url, json=None, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        if resp.status_code >= 400:
            return self._json(
                409,
                {
                    "title": (
                        "createCreditNote: the return is still a draft and could not be released"
                    ),
                    "detail": (
                        "A credit note may only be issued from a released return — otherwise "
                        "the return keeps no document number and can never be released again. "
                        "Releasing it first failed; fix that before settling the return."
                    ),
                },
            )
        return None

    async def _create_from_delivery_note(self, body, base_url, token, accept_language, client):  # noqa: ANN001
        """POST /api/v3/returnOrders/actions/createFromDeliveryNote — build a return
        from a delivery note. Maps the model command (deliveryNote + lines with
        deliveryNoteItem/quantity/reason) to the v3 wire body; returnReason is
        required on every line (upstream requirement)."""
        try:
            envelope = json.loads(body or b"{}")
        except (ValueError, TypeError):
            envelope = {}
        command = envelope.get("command") or {}
        dn = self._ref_id(command.get("deliveryNote"))
        lines = command.get("lineItems")
        if dn is None or not isinstance(lines, list) or not lines:
            return self._json(
                422,
                {
                    "title": (
                        "createFromDeliveryNote needs command.deliveryNote and "
                        "command.lineItems=[{deliveryNoteItem, quantity, reason}]"
                    )
                },
            )
        v3_lines: list[dict[str, Any]] = []
        for m in lines:
            if not isinstance(m, dict):
                continue
            item = self._ref_id(
                m.get("deliveryNoteItem") if m.get("deliveryNoteItem") is not None else m.get("id")
            )
            if item is None:
                return self._json(
                    422, {"title": f"createFromDeliveryNote: missing deliveryNoteItem in {m}"}
                )
            reason = self._ref_id(m.get("reason"))
            if reason is None:
                return self._json(
                    422,
                    {"title": f"createFromDeliveryNote: a ReturnReason is required per line ({m})"},
                )
            try:
                qty = float(m.get("quantity"))
            except (TypeError, ValueError):
                return self._json(422, {"title": f"createFromDeliveryNote: bad quantity in {m}"})
            li: dict[str, Any] = {"id": item["id"], "quantity": qty, "returnReason": reason}
            if m.get("description") is not None:
                li["description"] = m["description"]
            v3_lines.append(li)

        payload = {"deliveryNote": dn, "lineItems": v3_lines}
        url = f"{base_url.rstrip('/')}/api/v3/returnOrders/actions/createFromDeliveryNote"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001, ANN202
            return await c.post(url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            rbody = resp.json()
        except ValueError:
            rbody = {}
        if resp.status_code >= 400:
            return self._json(
                resp.status_code,
                rbody if isinstance(rbody, dict) else {"title": "createFromDeliveryNote failed"},
            )
        rec = rbody.get("data") if isinstance(rbody, dict) else None
        if isinstance(rec, dict):
            return self._json(201, {"data": self.map_read(rec)})
        return self._json(resp.status_code, rbody if isinstance(rbody, dict) else {})
