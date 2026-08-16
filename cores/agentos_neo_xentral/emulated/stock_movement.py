"""Xentral V3 facade · stockMovement — Lagerbewegung (docs/01-model.md §7.4, ADR-010).

READING now exists. ``GET /api/v3/stockMovements`` shipped with API-805 and closes
the single biggest gap this core carried (docs/05 #1) — the stock ledger was the
one core object with no read API at all. Live-verified against mvp on 2026-08-16:
the list answers 200, every declared filter was checked against the rows it
returned (not against its status), and every declared sort against a reversal.

What that measurement changed about the contract:

  * ``postedAt`` must be filtered as a DATETIME. A bare date answers 400, and the
    upstream compares the column itself so an index on ``zeit`` can be used — a
    date would wrap it in ``date(zeit)`` where no index can. ``base`` would trim
    the value if ``datetime_filters_take_date_only`` were set; it must NOT be.
  * The operator is ``greaterThanOrEquals``, plural. The OpenAPI example on the
    endpoint says ``greaterThanOrEqual`` and that is refused with a 400 naming the
    valid set — the example is wrong, reported back on API-805.
  * ``?search=`` is accepted and SILENTLY IGNORED (a term matching three rows
    answers with the same first page as no term at all), so it is not declared as
    a native search. ``source.reason``/``source.editor`` are searchable through the
    base's ``contains`` fan-out, which was verified to actually narrow the set.
  * ``page[size]`` caps at 100 and the envelope reports NO total, so lists carry
    page/perPage but no ``lastPage``. Reporting one would be invented.
  * ``warehouse`` filters through the storage location and is NOT echoed in the
    payload (verified: filter warehouse.id=9 returns location 3, whose warehouse
    is 9). It is a filter-only field here; the value stays null.

READING SINGLE: the upstream has no ``/{id}`` (404, deliberately out of scope on
the ticket), so ``read`` is composed from ``filter[id]`` — the substitute the
ticket itself names. Declared rather than left out: the emulation is exact, and a
caller that holds an id should not have to know it must build a list query.

WRITING is unchanged: the v1 storage-item endpoints book DELTA movements
(``POST/PATCH /v1/warehouses/{wh}/storageLocations/{loc}/items`` — add resp.
retrieve, incl. batch/bestBefore/serialNumbers/reason), so ``create`` is a real
write-orchestrator:

    receipt    -> add item to `to`
    issue      -> retrieve item from `from`
    transfer   -> retrieve from `from`, then add to `to` (compensated on a
                  partial failure — upstream has no atomic transfer)
    correction -> delta with mandatory reason; `to` books +, `from` books -

The model references product/location by our speaking ids; upstream wants
``product.sku`` and warehouse+location numerics, so the orchestrator resolves
both through this core's own Product/StorageLocation adapters.

A successful booking answers 201 with the echoed movement. Its ``id`` comes from
the upstream ``Location`` header — which the spec promises but mvp does NOT send
(verified 2026-07-31: 201, empty body, no header), so today the id stays null
rather than being invented. The handling is kept because it is spec-conformant
and starts working the day Xentral ships the header.

Consequence for callers: a booking still returns no identity of its own (the v1
item endpoints answer 201 with an empty body and mvp does not send the promised
header), so ``receipt``/``issue`` remain not retry-safe by construction, while
``correction`` + ``setQuantityTo`` is — it re-reads the location and books the
difference, so a repeat books zero. What the ledger changes is the aftermath: a
suspected double booking is now DETECTABLE, by listing the movements of that
product and location around the booking time. Before it could only be avoided.

The READ side reports one posting per stock change, which is not the grain of the
WRITE side: a ``transfer`` is one command and two postings, one ``outbound`` and
one ``inbound``. The read therefore reports ``direction`` and the single
``storageLocation`` it touched — ``type``/``from``/``to`` are create-only and stay
null, because deriving a ``type`` from a direction would invent the distinction
between a receipt, the arrival half of a transfer and a positive correction.
Upstream ``stockTransactionId`` is what ties the two halves back together, and it
is filled for ~0.1% of postings today (the stock management writing it is still
being rolled out) — so it is exposed and documented as such, not relied on.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import FacadeAdapterBase, REQUIRED, RO, eid, prop, ref
from .stock_shared import resolve_location_pair

_TYPE_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("receipt", "issue", "transfer", "correction")
]
_DIRECTION_OPTIONS = [
    {"value": "inbound", "label": "Inbound"},
    {"value": "outbound", "label": "Outbound"},
]
_TIMEOUT = 60.0

# Upstream `StockMovementCause` — what caused the posting. Deliberately broader
# than the business documents: measured across the fleet, stocktaking runs, goods
# posting documents and parcel receipts account for the majority of all postings,
# and mapping them through the document vocabulary alone reported 3.8 million of
# them as having no cause at all (API-805).
_CAUSE_OPTIONS = [
    {"value": v, "label": label}
    for v, label in (
        ("offer", "Offer"),
        ("salesOrder", "Sales order"),
        ("deliveryNote", "Delivery note"),
        ("invoice", "Invoice"),
        ("creditNote", "Credit note"),
        ("proformaInvoice", "Proforma invoice"),
        ("returnOrder", "Return order"),
        ("purchaseOrder", "Purchase order"),
        ("production", "Production"),
        ("inventoryRun", "Inventory run"),
        ("goodsPostingDocument", "Goods posting document"),
        ("parcelReceipt", "Parcel receipt"),
        ("serviceOrder", "Service order"),
    )
]

# Causes this core models as an entity of its own, so the ledger can hand out a
# record a caller can `get` straight away. The five that are missing
# (proformaInvoice, production, goodsPostingDocument, parcelReceipt, serviceOrder)
# report `type` + `id` and a null record — a speaking id pointing at an entity
# that does not exist would be worse than no link.
_CAUSE_RECORDS = {
    "offer": ("quo_", "quotes"),
    "salesOrder": ("so_", "salesOrders"),
    "deliveryNote": ("dn_", "deliveryNotes"),
    "invoice": ("si_", "salesInvoices"),
    "creditNote": ("cn_", "creditNotes"),
    "returnOrder": ("ret_", "returnOrders"),
    "purchaseOrder": ("po_", "purchaseOrders"),
    "inventoryRun": ("stk_", "stockTakes"),
}


def _ref_id(value: Any) -> str | None:
    """Speaking id out of a reference value ({'id': 'loc_3'} or 'loc_3')."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


def _id_from_location(header: str | None) -> str | None:
    """The created resource's id out of a ``Location`` header.

    The v1 item endpoints answer 200/201 with an EMPTY body and put the new
    resource's URI in ``Location`` (same shape as v2 products — see
    ProductAdapter._send). Only a numeric last segment is accepted: a header that
    points back at the collection (``…/items``) must not turn into a fabricated
    id like ``stm_items``.
    """
    if not header:
        return None
    tail = header.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


class StockMovementAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StockMovement",
        label_en="Stock movement",
        category="documents",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.stockMovement",
        source_apis=("agentos_neo_xentral",),
        # `read` is composed from filter[id] — the upstream has no /{id} route
        # (see the module docstring). `update`/`delete` stay undeclared: the ledger
        # is append-only upstream and there is nothing to route them to.
        operations=("list", "read", "create"),
        description=(
            "The warehouse ledger: one record per stock change of one product on "
            "one storage location. READ it to analyse movements (per product, "
            "location, warehouse, period or cause) and to verify what a booking "
            "did. To WRITE, PREFER the named StorageLocation actions (putaway / "
            "stockRemoval / stockTransfer / inventoryCount / stockAdjustment) — "
            "same orchestration as create here, but each with its own command "
            "schema and a stock-level read-back. Read and write do not share a "
            "grain: a transfer is one create and two ledger rows."
        ),
    )
    v3_path = "/api/v3/stockMovements"
    include = ""
    # Not the product name: the ledger returns the product as an id only and
    # hydrating a name would cost one call per row.
    preview_template = "{{source.reason}}"
    sections = {"general": {"label": "General"}, "source": {"label": "Source"}}
    # Model path -> upstream filter/sort key. The upstream names its references
    # with a `.id` suffix and calls the posting time `postedAt`.
    query_aliases = {
        "product": "product.id",
        "storageLocation": "storageLocation.id",
        "warehouse": "warehouse.id",
        "project": "project.id",
        "bookedAt": "postedAt",
        "source.reason": "reference",
        "source.editor": "editor",
    }
    # MUST stay False: this collection rejects a bare date on `postedAt` with a
    # 400, and filtering the datetime column directly is what lets an index on
    # `zeit` be used at all (SPS-147). Trimming the value here would break both.
    datetime_filters_take_date_only = False

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general", filterable=True, sortable=True),
            "direction": prop(
                "select",
                "Direction",
                **RO,
                section="general",
                options=_DIRECTION_OPTIONS,
                filterable=True,
                previewable=True,
            ),
            "type": prop(
                "select",
                "Type",
                **REQUIRED,
                section="general",
                options=_TYPE_OPTIONS,
                previewable=True,
                creatable=True,
            ),
            "product": prop(
                "reference",
                "Product",
                **REQUIRED,
                reference="Product",
                renderProperty="name",
                section="general",
                filterable=True,
                previewable=True,
                creatable=True,
            ),
            "quantity": prop(
                "embedded",
                "Quantity",
                section="general",
                creatable=True,
                sortable=True,
                properties={
                    "value": prop("decimal", "Value", creatable=True),
                    "unit": prop("string", "Unit", creatable=True),
                },
            ),
            "stockLevelAfter": prop("decimal", "Stock level after", **RO, section="general"),
            "storageLocation": prop(
                "reference",
                "Storage location",
                **RO,
                reference="StorageLocation",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                **RO,
                reference="Warehouse",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            "from": prop(
                "reference",
                "From location",
                reference="StorageLocation",
                renderProperty="name",
                section="general",
                creatable=True,
            ),
            "to": prop(
                "reference",
                "To location",
                reference="StorageLocation",
                renderProperty="name",
                section="general",
                creatable=True,
            ),
            "setQuantityTo": prop(
                "decimal",
                "Set quantity to",
                section="general",
                creatable=True,
                description=(
                    "Absolute correction (Inventur): sets the product's quantity on the "
                    "given location to this value — the difference is booked as a delta "
                    "movement. Use quantity for delta corrections instead. This is the "
                    "only RETRY-SAFE way to write stock: a repeat re-reads the location "
                    "and books zero, whereas repeating a receipt/issue books twice."
                ),
            ),
            "batch": prop(
                "reference",
                "Batch",
                reference="Batch",
                renderProperty="number",
                section="general",
                creatable=True,
            ),
            "unitCost": prop(
                "embedded",
                "Unit cost",
                **RO,
                section="general",
                properties={
                    "amount": prop("string", "Amount", **RO),
                    "currency": prop("string", "Currency", **RO),
                },
            ),
            "project": prop(
                "reference",
                "Project",
                **RO,
                reference="Project",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            "causedBy": prop(
                "embedded",
                "Caused by",
                **RO,
                section="source",
                properties={
                    "type": prop("select", "Type", **RO, options=_CAUSE_OPTIONS, filterable=True),
                    "id": prop("string", "ID", **RO, filterable=True),
                    "record": prop("reference", "Record", **RO, renderProperty="name"),
                },
            ),
            "stockMovementType": prop(
                "embedded",
                "Stock movement type",
                **RO,
                section="source",
                properties={"id": prop("string", "ID", **RO, filterable=True)},
            ),
            "systemType": prop("string", "System type", **RO, section="source"),
            "stockTransactionId": prop("string", "Stock transaction ID", **RO, section="source"),
            "source": prop(
                "embedded",
                "Source",
                section="source",
                creatable=True,
                properties={
                    "reason": prop(
                        "string", "Reason", creatable=True, filterable=True, searchable=True
                    ),
                    "editor": prop("string", "Editor", **RO, filterable=True, searchable=True),
                },
            ),
            "bookedAt": prop("datetime", "Booked at", **RO, filterable=True, sortable=True),
            "createdAt": prop("datetime", "Created at", **RO, filterable=True, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    @staticmethod
    def _node_id(node: Any) -> Any:
        """The bare id out of an upstream ``{"id": "…"}`` node, or None.

        `product`, `storageLocation` and `project` are all nullable upstream —
        the legacy columns are plain integers whose `0` placeholder is reported as
        no reference at all — so every one of these has to survive a null.
        """
        return node.get("id") if isinstance(node, dict) else None

    def _caused_by(self, r: dict[str, Any]) -> dict[str, Any] | None:
        cause = r.get("causedBy")
        if not isinstance(cause, dict):
            return None
        ctype, cid = cause.get("type"), cause.get("id")
        record = None
        known = _CAUSE_RECORDS.get(str(ctype))
        if known and cid not in (None, ""):
            record = ref(known[0], cid, None, None, known[1])
        return {
            "type": ctype,
            "id": (str(cid) if cid not in (None, "") else None),
            "record": record,
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        smt = self._node_id(r.get("stockMovementType"))
        return {
            "object": "stockMovement",
            "id": eid("stm_", r.get("id")),
            "direction": r.get("direction"),
            # Create-only vocabulary: a posting carries a direction, not a command.
            # See the module docstring on why this is not derived.
            "type": None,
            "product": ref("prd_", self._node_id(r.get("product")), None, None, "products"),
            # Signed by the direction upstream, so movements sum without a case
            # distinction. No unit is reported — reading one would cost a product
            # call per row.
            "quantity": {"value": r.get("quantity"), "unit": None},
            "stockLevelAfter": r.get("stockLevelAfter"),
            "storageLocation": ref(
                "loc_", self._node_id(r.get("storageLocation")), None, None, "storageLocations"
            ),
            # Filter-only: the ledger does not echo the warehouse (it is reached
            # through the storage location). Resolve it via `storageLocation`.
            "warehouse": None,
            "from": None,
            "to": None,
            "setQuantityTo": None,
            # Not reported by the ledger, though a booking can carry one.
            "batch": None,
            # Movement valuation is not exposed upstream at all (backlog blue wish).
            "unitCost": {"amount": None, "currency": None},
            "project": ref("prj_", self._node_id(r.get("project")), None, None, "projects"),
            "causedBy": self._caused_by(r),
            "stockMovementType": ({"id": str(smt)} if smt not in (None, "") else None),
            "systemType": r.get("systemType"),
            "stockTransactionId": r.get("stockTransactionId"),
            "source": {
                # Upstream `reference` is the same free text a create writes as
                # `source.reason`, so a booking reads back where it was written.
                "reason": r.get("reference") or None,
                # A username as stored, not a resolvable user record.
                "editor": r.get("editor") or None,
            },
            "bookedAt": r.get("postedAt"),
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # ---- write orchestration (v1 storage-item endpoints) -------------------
    async def request(
        self, *, method, handle, query, body, base_url, token, accept_language=None, client=None
    ) -> AdapterResponse:
        if method.upper() == "POST":
            return await self._create_movement(
                query, body, base_url, token, accept_language, client
            )
        if method.upper() == "GET" and handle and "read" in self.manifest.operations:
            return await self._read_one(handle, base_url, token, accept_language, client)
        return await super().request(
            method=method,
            handle=handle,
            query=query,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )

    async def _read_one(
        self, handle: str, base_url, token, accept_language, client
    ) -> AdapterResponse:
        """Single read through ``filter[id]`` — the upstream has no ``/{id}``.

        ``GET /api/v3/stockMovements/{id}`` answers 404: the ticket left the detail
        route out of scope and named the id filter as the way to reach one record.
        Emulating it here keeps the contract whole; going through ``_get`` with a
        handle would build exactly the 404 route.
        """
        numeric = handle.split("_", 1)[1] if "_" in handle else handle
        if not numeric.isdigit():
            return self._json(
                422,
                {
                    "title": f"stockMovement: malformed id {handle!r}",
                    "detail": "Expected stm_<numeric ledger id>.",
                },
            )
        status, payload = await self._get(
            base_url,
            token,
            handle=None,
            query=[
                ("filter[0][key]", "id"),
                ("filter[0][op]", "equals"),
                ("filter[0][value]", numeric),
                ("page[number]", "1"),
                ("page[size]", "1"),
            ],
            accept_language=accept_language,
            client=client,
        )
        if status >= 400:
            return self._json(
                status, payload if isinstance(payload, dict) else {"title": "upstream error"}
            )
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        if not rows or not isinstance(rows[0], dict):
            return self._json(404, {"title": f"stockMovement {handle} not found"})
        return self._json(200, {"data": self.map_read(rows[0])})

    async def _lookup(self, adapter, handle, base_url, token, accept_language, client):
        resp = await adapter.request(
            method="GET",
            handle=handle,
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
            return json.loads(resp.content or b"{}").get("data") or None
        except ValueError:
            return None

    async def _resolve_location(
        self, loc_id: str, base_url, token, accept_language, client
    ) -> tuple[str, str] | None:
        """``loc_…`` -> (warehouseId, storageLocationId) numerics."""
        return await resolve_location_pair(
            loc_id,
            base_url=base_url,
            headers=self._headers(token, accept_language),
            client=client,
        )

    async def _items_call(
        self,
        base_url,
        token,
        accept_language,
        client,
        *,
        method: str,
        warehouse_id: str,
        location_id: str,
        payload: dict[str, Any],
    ) -> tuple[int, Any, str | None]:
        """Returns ``(status, body, locationHeader)`` — the header carries the
        created resource's URI and is the ONLY identity these endpoints emit."""
        url = (
            f"{base_url.rstrip('/')}/api/v1/warehouses/{warehouse_id}"
            f"/storageLocations/{location_id}/items"
        )
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        location = resp.headers.get("Location") or resp.headers.get("location")
        try:
            return resp.status_code, resp.json(), location
        except ValueError:
            return resp.status_code, {}, location

    async def _location_quantity(
        self, loc: tuple[str, str], sku: str, base_url, token, accept_language, client
    ) -> float | None:
        """Current quantity of a product on one storage location, via
        ``GET /v2/warehouses/{wh}/storageLocations/{loc}/items`` (rows carry
        top-level ``sku``/``productId``; paginated, no sku filter upstream —
        pages are scanned until ``extra.totalCount`` is exhausted). ``None``
        when the contents are unreadable."""
        url = f"{base_url.rstrip('/')}/api/v2/warehouses/{loc[0]}/storageLocations/{loc[1]}/items"
        headers = self._headers(token, accept_language)
        total = 0.0
        page = 1
        seen = 0
        while page <= 100:  # 5000 rows — far beyond any sane single location
            params = [("page[number]", str(page)), ("page[size]", "50")]

            async def _do(c: httpx.AsyncClient, p=tuple(params)) -> httpx.Response:
                return await c.get(url, params=list(p), headers=headers)

            if client is None:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                    resp = await _do(c)
            else:
                resp = await _do(client)
            if resp.status_code >= 400:
                return None
            try:
                body = resp.json()
            except ValueError:
                return None
            rows = body.get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_sku = row.get("sku")
                if row_sku is None or str(row_sku) != str(sku):
                    continue  # strict match only — never sum unidentified rows
                try:
                    total += float(row.get("quantity"))
                except (TypeError, ValueError):
                    continue
            seen += len(rows)
            total_count = ((body.get("extra") or {}).get("totalCount")) or 0
            if not rows or seen >= int(total_count):
                break
            page += 1
        return total

    async def _create_movement(
        self, query, body, base_url, token, accept_language, client
    ) -> AdapterResponse:
        try:
            model = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return self._json(400, {"title": "invalid JSON body"})
        if not isinstance(model, dict):
            return self._json(400, {"title": "body must be a JSON object"})

        mtype = model.get("type")
        qty = model.get("quantity")
        qty_value = qty.get("value") if isinstance(qty, dict) else qty
        set_to = model.get("setQuantityTo")
        product_id = _ref_id(model.get("product"))
        from_id = _ref_id(model.get("from"))
        to_id = _ref_id(model.get("to"))
        reason = (
            (model.get("source") or {}).get("reason")
            if isinstance(model.get("source"), dict)
            else None
        )
        absolute = mtype == "correction" and set_to is not None

        problems: list[str] = []
        if mtype not in ("receipt", "issue", "transfer", "correction"):
            problems.append("type must be receipt|issue|transfer|correction")
        qty_num = 0.0
        if absolute:
            if qty_value is not None:
                problems.append("correction takes quantity (delta) OR setQuantityTo, not both")
            try:
                set_to_num = float(set_to)
                if set_to_num < 0:
                    problems.append("setQuantityTo must be >= 0")
            except (TypeError, ValueError):
                set_to_num = 0.0
                problems.append("setQuantityTo must be a number")
        else:
            try:
                qty_num = float(qty_value)
                if qty_num <= 0:
                    problems.append("quantity.value must be > 0")
            except (TypeError, ValueError):
                problems.append("quantity.value must be a number")
        if not product_id:
            problems.append("product reference is required")
        if mtype == "receipt" and not to_id:
            problems.append("receipt needs a 'to' location")
        if mtype == "issue" and not from_id:
            problems.append("issue needs a 'from' location")
        if mtype == "transfer" and not (from_id and to_id):
            problems.append("transfer needs 'from' AND 'to' locations")
        if mtype == "correction":
            if not reason:
                problems.append("correction needs source.reason")
            if absolute:
                if not (bool(from_id) ^ bool(to_id)):
                    problems.append("setQuantityTo needs exactly ONE location (to or from)")
            elif bool(from_id) == bool(to_id):
                problems.append("correction needs exactly ONE of 'to' (+) or 'from' (-)")
        if problems:
            return self._json(
                422, {"title": "stockMovement: invalid booking", "problems": problems}
            )

        # Resolve model references to the upstream's identifiers.
        from .product import ProductAdapter

        product = await self._lookup(
            ProductAdapter(), product_id, base_url, token, accept_language, client
        )
        sku = (product or {}).get("number")
        if not sku:
            return self._refuse(422, f"product {product_id}: no SKU (number) resolvable")

        async def location(loc_id: str | None) -> tuple[str, str] | None:
            if not loc_id:
                return None
            return await self._resolve_location(loc_id, base_url, token, accept_language, client)

        src = await location(from_id)
        dst = await location(to_id)
        if from_id and src is None:
            return self._refuse(422, f"location {from_id}: not resolvable (warehouse?)")
        if to_id and dst is None:
            return self._refuse(422, f"location {to_id}: not resolvable (warehouse?)")

        # Absolute correction (setQuantityTo, Inventur): NOT via the upstream
        # setTotalStock endpoint — that call REMOVES every other product/batch
        # on the location that isn't repeated in its payload (spec CAUTION).
        # Instead: read the location's current quantity and book the difference
        # as a delta — same audit-friendly path, no wipe hazard.
        current: float | None = None
        if absolute:
            loc_pair = dst or src
            current = await self._location_quantity(
                loc_pair, str(sku), base_url, token, accept_language, client
            )
            if current is None:
                return self._json(
                    502, {"title": "setQuantityTo: current location stock not readable"}
                )
            qty_num = abs(set_to_num - current)

        payload: dict[str, Any] = {"product": {"sku": str(sku)}, "quantity": qty_num}
        batch = model.get("batch")
        batch_number = batch.get("number") if isinstance(batch, dict) else batch
        if batch_number:
            payload["batch"] = str(batch_number)
        if reason:
            payload["reason"] = str(reason)

        steps: list[tuple[str, tuple[str, str]]] = []  # (http method, (wh, loc))
        if absolute:
            if qty_num != 0:
                loc_pair = dst or src
                steps = [("POST" if set_to_num > (current or 0) else "PATCH", loc_pair)]  # type: ignore[list-item]
        elif mtype == "receipt" or (mtype == "correction" and dst):
            steps = [("POST", dst)]  # type: ignore[list-item]
        elif mtype == "issue" or (mtype == "correction" and src):
            steps = [("PATCH", src)]  # type: ignore[list-item]
        elif mtype == "transfer":
            steps = [("PATCH", src), ("POST", dst)]  # type: ignore[list-item]

        if any(k == "dryRun" and v in ("true", "1") for k, v in query):
            return self._json(
                200,
                {
                    "data": {
                        "dryRun": True,
                        **(
                            {"currentQuantity": current, "targetQuantity": set_to_num}
                            if absolute
                            else {}
                        ),
                        "wouldBook": [
                            {
                                "method": m,
                                "warehouseId": loc[0],
                                "storageLocationId": loc[1],
                                "payload": payload,
                            }
                            for m, loc in steps
                        ],
                    }
                },
            )

        done: list[tuple[str, tuple[str, str]]] = []
        booked_id: str | None = None
        for http_method, loc in steps:
            st, resp, location_header = await self._items_call(
                base_url,
                token,
                accept_language,
                client,
                method=http_method,
                warehouse_id=loc[0],
                location_id=loc[1],
                payload=payload,
            )
            if st >= 400:
                detail = resp if isinstance(resp, dict) else {}
                # transfer: the retrieve succeeded but the add failed — put the
                # units back so nothing is left "in the air" (best effort).
                if done:
                    prev_method, prev_loc = done[-1]
                    comp_method = "POST" if prev_method == "PATCH" else "PATCH"
                    comp_st, _, _ = await self._items_call(
                        base_url,
                        token,
                        accept_language,
                        client,
                        method=comp_method,
                        warehouse_id=prev_loc[0],
                        location_id=prev_loc[1],
                        payload=payload,
                    )
                    detail["compensation"] = (
                        "reverted" if comp_st < 400 else "FAILED — check stock manually"
                    )
                return self._json(
                    st,
                    {
                        "title": f"stockMovement: upstream booking failed ({http_method})",
                        **detail,
                    },
                )
            # The LAST successful call owns the identity: a transfer books out and
            # then in, and the record this facade returns is the arrival.
            booked_id = _id_from_location(location_header) or booked_id
            done.append((http_method, loc))

        # Identity comes from the upstream Location header — the item endpoints
        # answer with an empty body. Without it the id stays null rather than
        # being invented; a caller can then tell "no identity" from "id 0".
        echo = {
            **model,
            "object": "stockMovement",
            "id": (f"stm_{booked_id}" if booked_id else None),
        }
        return self._json(201, {"data": echo})
