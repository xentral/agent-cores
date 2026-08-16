"""Xentral V3 facade · storageLocation — Lagerplatz (docs/01-model.md §7.5).

Reads ``GET /v1/storageLocations`` (verified live — thin: id, designation,
warehouse) and fills every characterising field from the warehouse-scoped
``/v1/warehouses/{id}/storageLocations``, which is the only endpoint that returns
them. ``contents`` is answered by the StockLevel projection
(filter[storageLocation]).

Xentral names its bin properties after its own history — Nachschublager,
Verbrauchslager, Sperrlager, Fertigungszugriff, Kassenplatz. They are five
INDEPENDENT booleans, so this model exposes them as a ``usage`` block rather than
the single-valued ``kind`` it used to declare, four of whose five values existed
nowhere upstream. Alongside them: ``abcCategory`` (picking priority),
``pickingOrder`` (upstream ``sort``) and physical ``dimensions``.

This is also WHERE WAREHOUSE WORK HAPPENS: the five stock actions (putaway,
stockRemoval, stockTransfer, inventoryCount, stockAdjustment) hang off the bin,
because that is what a warehouse acts on. They are the named entry points to the
booking orchestration in ``stock_movement`` — one implementation, addressed by
purpose instead of by a type discriminator plus a field combination.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import FacadeAdapterBase, REQUIRED, RO, id_from_location, prop, ref, _TIMEOUT

_CU = {"creatable": True, "updatable": True}


class StorageLocationAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StorageLocation",
        label_en="Storage location",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.storageLocation",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
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
                        wish=True,
                    ),
                    self.step_cmd(
                        "release",
                        "Release",
                        wish=True,
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
            self.action_def("printLabel", "Print label", wish=True),
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
                **REQUIRED,
                section="general",
                creatable=True,
                updatable=True,
                filterable=True,
                previewable=True,
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                **REQUIRED,
                reference="Warehouse",
                renderProperty="name",
                section="general",
                previewable=True,
                creatable=True,
                filterable=True,
                description=(
                    "The warehouse this location belongs to. Filtering by it reads the "
                    "warehouse-scoped upstream collection instead of paging the tenant. "
                    "REQUIRED on create: upstream has no standalone storage-location "
                    "endpoint — a location is created underneath its warehouse."
                ),
            ),
            # Upstream models the role as FIVE INDEPENDENT FLAGS (StorageType), not
            # one exclusive kind: a bin can be a replenishment location AND blocked
            # at the same time. The old single-valued `kind`
            # (picking|bulk|inbound|returns|quarantine) could express none of that,
            # and four of its five values do not exist upstream at all — they were
            # invented here. Named for what a warehouse consultant asks for.
            "usage": prop(
                "embedded",
                "Usage",
                section="general",
                properties={
                    "replenishment": prop(
                        "boolean",
                        "Replenishment location",
                        **_CU,
                        description="Nachschubplatz: stock is pulled from here to refill picking bins.",
                    ),
                    "consumption": prop(
                        "boolean",
                        "Consumption location",
                        **_CU,
                        description="Verbrauchsplatz: stock booked out here is consumed, not shipped.",
                    ),
                    "blocked": prop(
                        "boolean",
                        "Blocked stock",
                        **_CU,
                        description=(
                            "Sperrplatz: quarantine/quality hold — its stock is excluded from "
                            "availability and auto-shipping. Same upstream flag the `status` "
                            "field reports as blocked/active."
                        ),
                    ),
                    "production": prop(
                        "boolean",
                        "Production access",
                        **_CU,
                        description="Fertigung darf von hier entnehmen.",
                    ),
                    "pointOfSale": prop(
                        "boolean",
                        "Point of sale",
                        **_CU,
                        description="Kassenplatz: stock sold over the counter.",
                    ),
                },
            ),
            "abcCategory": prop(
                "select",
                "ABC category",
                **_CU,
                section="general",
                options=[{"value": v, "label": v} for v in ("A", "B", "C")],
                description=(
                    "ABC classification driving picking priority — A = fastest movers, "
                    "nearest the packing bench."
                ),
            ),
            "pickingOrder": prop(
                "integer",
                "Picking order",
                **_CU,
                section="general",
                description="Sequence a picker walks the bins in (upstream `sort`).",
            ),
            "dimensions": prop(
                "embedded",
                "Dimensions",
                section="general",
                description=(
                    "Physical size of the bin. Upstream carries length/width/height only — "
                    "there is no weight limit, and the `capacity.maxWeight` this model used "
                    "to declare did not exist anywhere."
                ),
                properties={
                    "length": prop("decimal", "Length", **_CU),
                    "width": prop("decimal", "Width", **_CU),
                    "height": prop("decimal", "Height", **_CU),
                },
            ),
            # Read-only on purpose: upstream returns the field but rejects it on
            # BOTH create and update with "The attribute description is not
            # allowed." (measured on mvp 2026-08-02). Declaring it writable would
            # make every write that carries it fail with a 400.
            "description": prop("string", "Description", **RO, section="general"),
            "contents": prop(
                "collection",
                "Contents",
                **RO,
                section="contents",
                description=(
                    "What lies on this location, composed from StockLevel. Filled on a "
                    "SINGLE read only — on a list it would cost one extra call per row. "
                    "Use StockLevel with filter[storageLocation] to query it directly. "
                    "reserved stays empty: no per-location reservation exists upstream."
                ),
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

    async def _warehouse_of(self, handle, base_url, token, accept_language, client):  # noqa: ANN001
        """Which warehouse a location belongs to.

        Needed because the write routes are nested under the warehouse and there is
        NO detail read to ask: both `GET /v1/storageLocations/{id}` and the nested
        `GET /v1/warehouses/{w}/storageLocations/{id}` answer 404. The flat list
        carries `warehouse` on every row, so it is swept until the id turns up.
        """
        for page in range(1, 21):
            st, payload = await self._get(
                base_url,
                token,
                handle=None,
                query=[("page[number]", str(page)), ("page[size]", "100")],
                accept_language=accept_language,
                client=client,
            )
            rows = (payload or {}).get("data") or []
            if st >= 400 or not rows:
                return None
            for row in rows:
                if str(row.get("id")) == str(handle):
                    wh = row.get("warehouse")
                    return str(wh.get("id")) if isinstance(wh, dict) else (str(wh) if wh else None)
        return None

    async def _send(  # noqa: ANN001
        self, base_url, token, method, up_handle, payload, accept_language, client
    ):
        """Writes go to the warehouse-scoped sub-resource.

        `POST/PATCH/DELETE /api/v1/warehouses/{warehouseId}/storageLocations[/{id}]`
        is the only write surface: there is no standalone one. The entity API's
        `storageLocation` looks like one in the catalogue but has no warehouse field,
        so a location created there is an orphan our own list never returns — swept
        every page, detail read 404.
        """
        method = str(method).upper()
        wh = None
        if method == "POST":
            wh = (payload or {}).pop("__warehouse", None)
            if not wh:
                return 422, {
                    "title": "storageLocation: warehouse is required",
                    "detail": (
                        "A storage location is created underneath its warehouse; "
                        "upstream has no standalone endpoint. Set `warehouse`."
                    ),
                }
        else:
            wh = await self._warehouse_of(up_handle, base_url, token, accept_language, client)
            if not wh:
                return 404, {"title": f"storageLocation {up_handle}: warehouse not resolvable"}
        path = f"/api/v1/warehouses/{wh}/storageLocations" + (f"/{up_handle}" if up_handle else "")
        url = f"{base_url.rstrip('/')}{path}"
        headers = self._headers(token, accept_language)

        async def _do(c):  # noqa: ANN001
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            out = resp.json()
        except ValueError:
            out = {}
        if resp.status_code < 400 and not (
            isinstance(out, dict) and (out.get("data") or {}).get("id")
        ):
            new_id = id_from_location(resp.headers.get("Location") or resp.headers.get("location"))
            if new_id:
                out = {"data": {"id": new_id}}
        return resp.status_code, out

    def map_write(self, model, *, creating):  # noqa: ANN001
        """`warehouse` travels as `__warehouse` — it is part of the PATH, not the body."""
        wire = {}
        rejected = set()
        if "name" in model:
            wire["designation"] = model["name"]
        wh = model.get("warehouse")
        wh_id = wh.get("id") if isinstance(wh, dict) else wh
        if wh_id:
            wire["__warehouse"] = str(wh_id).split("_", 1)[-1]
        usage = model.get("usage") or {}
        for mine, theirs in (
            ("replenishment", "isReplenishmentLocation"),
            ("consumption", "isConsumptionLocation"),
            ("blocked", "isRestrictedLocation"),
            # asymmetric on purpose: read name is `productionAccess`
            ("production", "allowsProductionAccess"),
            ("pointOfSale", "isPosLocation"),
        ):
            if mine in usage:
                wire[theirs] = bool(usage[mine])
        if "abcCategory" in model:
            wire["abcCategory"] = model["abcCategory"]
        if "pickingOrder" in model:
            wire["sort"] = model["pickingOrder"]
        dims = model.get("dimensions") or {}
        if dims:
            wire["dimensions"] = {
                k: v
                for k, v in dims.items()
                if k in ("length", "width", "height") and v is not None
            }
        for path in ("contents", "status", "id", "object", "description"):
            if path in model:
                rejected.add(path)
        return wire, rejected

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
            # Upstream returns these on the WAREHOUSE-SCOPED list only; the flat
            # tenant-wide /v1/storageLocations answers id/designation/warehouse and
            # nothing else. `_enrich` fills them in for every read path, so this
            # mapping sees them — but only because of that extra fetch.
            "usage": {
                "replenishment": r.get("isReplenishmentLocation"),
                "consumption": r.get("isConsumptionLocation"),
                "blocked": r.get("isRestrictedLocation"),
                # Upstream READS this as `productionAccess` and WRITES it as
                # `allowsProductionAccess` — measured, not assumed.
                "production": r.get("productionAccess"),
                "pointOfSale": r.get("isPosLocation"),
            },
            "abcCategory": r.get("abcCategory") or None,
            "pickingOrder": r.get("sort"),
            "dimensions": {
                "length": (r.get("dimensions") or {}).get("length"),
                "width": (r.get("dimensions") or {}).get("width"),
                "height": (r.get("dimensions") or {}).get("height"),
            },
            "description": r.get("description") or None,
            "contents": [],
            "createdAt": None,
            "updatedAt": None,
        }

    # ---- reads: warehouse scope + contents ---------------------------------
    async def request(  # noqa: ANN001
        self, *, method, handle, query, body, base_url, token, accept_language=None, client=None
    ) -> AdapterResponse:
        """Two enrichments over the plain v1 list:

        * ``filter[warehouse]`` — the flat ``/v1/storageLocations`` cannot filter
          by warehouse, but the upstream list is warehouse-scoped by nature
          (``/v1/warehouses/{id}/storageLocations``). Without this, finding the
          bins of one warehouse means paging the whole tenant and filtering by
          hand — 3 pages over 102 rows on mvp just to reach 9.
        * ``contents`` on a SINGLE read — composed from the StockLevel
          projection. Not on the list: that would be one extra call per row.
        * every characterising field (``usage``, ``abcCategory``, ``pickingOrder``,
          ``dimensions``) on EVERY read path — see :meth:`_enrich`. One extra call
          per distinct warehouse on the page, not per row.
        """
        if method.upper() != "GET":
            resp = await super().request(
                method=method,
                handle=handle,
                query=query,
                body=body,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
            # The base reconciles a write by re-reading through the plain list,
            # which for this entity is the THIN one — a caller who wrote an ABC
            # class or a usage flag would get back a record that does not show it,
            # and could reasonably conclude the write was lost. Re-read through
            # our own GET so a write answers exactly what a read answers.
            return await self._reread(resp, base_url, token, accept_language, client)
        from .stock_shared import numeric, parse_filters, resolve_location_row

        if handle is None:
            warehouse = parse_filters(query).get("warehouse")
            if warehouse:
                return await self._by_warehouse(
                    numeric(str(warehouse[1])), query, base_url, token, accept_language, client
                )
            listing = await super().request(
                method=method,
                handle=handle,
                query=query,
                body=body,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
            return await self._enrich_listing(listing, base_url, token, accept_language, client)
        # Single read: v1 has NO show route — GET /v1/storageLocations/{id} answers
        # 404 (verified on mvp 2026-07-31), so the entity declared `read` and could
        # not do it. The id filter on the list is the lookup this core already uses
        # to resolve a location for a booking; reuse it instead of dropping `read`.
        row = await resolve_location_row(
            handle, base_url=base_url, headers=self._headers(token, accept_language), client=client
        )
        if row is None:
            return self._json(404, {"title": f"StorageLocation {handle} not found"})
        # The id-filtered flat list answers designation + warehouse and NOTHING else
        # — no usage flags, no ABC class, no dimensions. Those live on the
        # warehouse-scoped collection, so a single read fetches the row again from
        # there. One extra call, and it is the difference between a record that
        # describes the bin and one that only names it.
        (row,) = await self._enrich([row], base_url, token, accept_language, client)
        resp = self._json(200, {"data": self.map_read(row)})
        return await self._with_contents(resp, handle, base_url, token, accept_language, client)

    # fields the flat list cannot answer, so they are overlaid from the scoped one
    _SCOPED_ONLY = ("usage", "abcCategory", "pickingOrder", "dimensions", "description", "status")

    async def _enrich_listing(  # noqa: ANN001
        self, resp, base_url, token, accept_language, client
    ) -> AdapterResponse:
        """Overlay the scoped-only fields onto an already-mapped listing."""
        if resp.status_code != 200:
            return resp
        try:
            payload = json.loads(resp.content or b"{}")
        except ValueError:
            return resp
        mapped = payload.get("data") or []
        if not isinstance(mapped, list) or not mapped:
            return resp
        thin = [
            {
                "id": str(r.get("id") or "").removeprefix("loc_"),
                "warehouse": {
                    "id": str((r.get("warehouse") or {}).get("id") or "").removeprefix("wh_")
                },
            }
            for r in mapped
        ]
        by_id = {
            str(r["id"]): r
            for r in await self._enrich(thin, base_url, token, accept_language, client)
        }
        for row in mapped:
            upstream = by_id.get(str(row.get("id") or "").removeprefix("loc_"))
            # `_enrich` leaves a row untouched when its warehouse is unreachable;
            # such a row still only carries the two flat keys, and overlaying its
            # mapping would write nulls over nothing. Skip it instead.
            if not upstream or "designation" not in upstream:
                continue
            fresh = self.map_read(upstream)
            for key in self._SCOPED_ONLY:
                row[key] = fresh[key]
        return self._json(200, payload)

    async def _reread(  # noqa: ANN001
        self, resp, base_url, token, accept_language, client
    ) -> AdapterResponse:
        """Replace a 2xx write body with a full read of the written record."""
        if resp.status_code >= 300 or resp.status_code == 204:
            return resp
        try:
            written = (json.loads(resp.content or b"{}").get("data") or {}).get("id")
        except (ValueError, AttributeError):
            return resp
        if not written:
            return resp
        fresh = await self.request(
            method="GET",
            handle=str(written),
            query=[],
            body=None,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        # A failed re-read is not a failed write: keep the original answer.
        return fresh if fresh.status_code < 300 else resp

    async def _enrich(  # noqa: ANN001
        self, rows, base_url, token, accept_language, client
    ):
        """Fill thin rows from their warehouse-scoped collections.

        The flat tenant-wide list answers ``id``/``designation``/``warehouse``;
        every field that CHARACTERISES a bin — the usage flags, the ABC class, the
        picking order, the dimensions — is returned only by
        ``/v1/warehouses/{id}/storageLocations``. A list built on the flat endpoint
        alone can name bins but not describe them, which is the wrong half for
        "show me the blocked ones".

        Cost is one request per DISTINCT warehouse on the page (concurrent), not
        one per row. Rows are enriched all-or-nothing per warehouse: a partially
        enriched page would mix a real ``false`` with a not-fetched ``None`` and no
        caller could tell them apart.
        """
        from .stock_shared import get_json

        by_wh: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            wh = row.get("warehouse")
            wid = wh.get("id") if isinstance(wh, dict) else wh
            if wid:
                by_wh.setdefault(str(wid), []).append(row)
        if not by_wh:
            return rows

        async def scoped(wid: str) -> dict[str, dict[str, Any]]:
            """Every location of one warehouse, by id — paged until exhausted."""
            found: dict[str, dict[str, Any]] = {}
            page = 1
            while page <= 20:  # 20 * 50 = 1000 bins per warehouse
                status, payload = await get_json(
                    f"{base_url.rstrip('/')}/api/v1/warehouses/{wid}/storageLocations",
                    [("page[number]", str(page)), ("page[size]", "50")],
                    self._headers(token, accept_language),
                    client,
                )
                got = (payload.get("data") if isinstance(payload, dict) else None) or []
                if status >= 400 or not got:
                    break
                for r in got:
                    found[str(r.get("id"))] = r
                if len(got) < 50:
                    break
                page += 1
            return found

        ids = list(by_wh)
        results = await asyncio.gather(*(scoped(w) for w in ids), return_exceptions=True)
        for wid, scoped_rows in zip(ids, results, strict=True):
            if isinstance(scoped_rows, BaseException) or not scoped_rows:
                continue  # unreachable warehouse: leave those rows thin, never fail
            for row in by_wh[wid]:
                rich = scoped_rows.get(str(row.get("id")))
                if rich:
                    # the scoped row omits the warehouse the flat one carries
                    keep = row.get("warehouse")
                    row.update(rich)
                    row["warehouse"] = keep or rich.get("warehouse")
        return rows

    async def _by_warehouse(  # noqa: ANN001
        self, warehouse_id, query, base_url, token, accept_language, client
    ) -> AdapterResponse:
        from .stock_shared import get_json

        page, per_page = self._query_paging(query)
        size = max(10, min(50, per_page))  # v1 rejects sizes outside 10..50
        status, payload = await get_json(
            f"{base_url.rstrip('/')}/api/v1/warehouses/{warehouse_id}/storageLocations",
            [("page[number]", str(page)), ("page[size]", str(size))],
            self._headers(token, accept_language),
            client,
        )
        if status >= 400:
            return self._json(
                status,
                {
                    "title": f"storageLocation: warehouse {warehouse_id} not readable",
                    "detail": payload if isinstance(payload, dict) else None,
                },
            )
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        mapped = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # The scoped collection may omit the warehouse (it is implied by the
            # path); the model always carries it, so put it back.
            if not row.get("warehouse"):
                row = {**row, "warehouse": {"id": warehouse_id}}
            mapped.append(self.map_read(row))
        return self._json(200, self._list_envelope(mapped, payload, query))

    async def _with_contents(  # noqa: ANN001
        self, resp, handle, base_url, token, accept_language, client
    ) -> AdapterResponse:
        """``contents`` = what StockLevel reports for this location."""
        from .stock_level import StockLevelAdapter

        try:
            body = json.loads(resp.content or b"{}")
        except ValueError:
            return resp
        record = body.get("data") if isinstance(body, dict) else None
        if not isinstance(record, dict):
            return resp
        levels = await StockLevelAdapter().request(
            method="GET",
            handle=None,
            query=[
                ("filter[0][key]", "storageLocation"),
                ("filter[0][op]", "equals"),
                ("filter[0][value]", str(handle)),
                ("page[number]", "1"),
                ("page[size]", "50"),
            ],
            body=None,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if levels.status_code >= 400:
            return resp  # leave contents empty rather than claim the bin is empty
        try:
            rows = json.loads(levels.content or b"{}").get("data") or []
        except ValueError:
            return resp
        record["contents"] = [
            {
                "product": r.get("product"),
                "batch": r.get("batch"),
                "quantity": r.get("quantity"),
                "reserved": r.get("reserved"),
            }
            for r in rows
            if isinstance(r, dict)
        ]
        return self._json(200, body)

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

    async def _read_level(  # noqa: ANN001
        self, product: str, location: str, base_url, token, accept_language, client
    ) -> dict[str, Any] | None:
        """The stock level of one product on one location after the booking —
        the read-back every write owes its caller (ADR-018).

        Emptying a location REMOVES the row upstream, so the read then 404s.
        "No row" and "zero on the shelf" are the same fact, and a null right
        after a successful booking reads as a failed one (observed live: a
        transfer that emptied its source answered ``data: null``). A 404 is
        therefore reported as an explicit zero level — while any OTHER read
        error stays ``None``, because "unreadable" must never be served as
        "zero", which is exactly the number a caller would act on.
        """
        from .stock_level import StockLevelAdapter
        from .stock_shared import numeric, resolve_location_row

        product_id, location_id = numeric(str(product)), numeric(str(location))
        adapter = StockLevelAdapter()
        resp = await adapter.request(
            method="GET",
            handle=f"slv_{product_id}_{location_id}",
            query=[],
            body=None,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
        if resp.status_code == 404:
            # A zero row must be shaped like a real one, or "same fact" is only
            # half true: the location row still carries its designation and
            # warehouse, and a caller reading `warehouse` off the result would
            # find it missing on exactly the records that report an empty bin.
            row = await resolve_location_row(
                location_id,
                base_url=base_url,
                headers=self._headers(token, accept_language),
                client=client,
            )
            wh = (row or {}).get("warehouse") or {}
            return adapter.level_row(
                product_id=product_id,
                location_id=location_id,
                location_name=(row or {}).get("designation"),
                warehouse_id=wh.get("id") if isinstance(wh, dict) else None,
                warehouse_name=wh.get("name") if isinstance(wh, dict) else None,
                quantity=0,
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
            "data": await self._read_level(
                product, location, base_url, token, accept_language, client
            ),
            "result": {"action": action_key, "storageLocation": location},
        }
        if action_key == "stockTransfer":
            out["result"]["target"] = await self._read_level(
                product, str(model["to"]), base_url, token, accept_language, client
            )
        return self._json(200, out)
