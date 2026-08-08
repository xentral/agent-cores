"""Xentral V3 facade · stockLevel — Bestand (docs/01-model.md §7.5).

The stock query of the model: a read-only projection of product × storage
location with ``quantity``. There is still no unified
``GET /v3/stockLevels?filter[…]`` upstream (docs/05 #18) — but the data is
readable today through two scattered endpoints, and THIS adapter composes them:

    filter[product]         -> GET /v1/products/{id}/storageLocations
    filter[storageLocation] -> GET /v2/warehouses/{wh}/storageLocations/{loc}/items

Both answer the same grain (product × location) from opposite directions, so one
projection covers both. ``warehouse`` narrows either result set client-side.

Why one of the two filters is MANDATORY: neither endpoint can be addressed
without its anchor, and there is no global stock collection upstream. Answering
an unfiltered list would mean fanning out over every product (or every location)
of the tenant — thousands of calls for one list. The request is refused with 422
naming the supported combinations instead, so a caller learns the contract from
the error rather than from a timeout.

This is the read-back path for stock writes: ``stockMovement`` books, this reads
the effect. Without it a booking is unverifiable, because the movement ledger
itself has no read API at all (docs/05 #1).

Not covered at this grain: ``reserved``/``available`` (no per-location reservation
is exposed upstream — only the product totals in ``/v1/products/{id}/stocks``) and
the batch split (the v2 items rows carry ``qualityControlAttributes``, the product
endpoint does not — emitting batch rows on one path only would make the grain
depend on the filter). Both stay blue wishes in erp-spec.yaml.
"""

from __future__ import annotations

from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref
from .stock_shared import get_json, numeric, parse_filters, resolve_location_row

_MAX_PAGES = 100
_PAGE_SIZE = 50


class StockLevelAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="StockLevel",
        label_en="Stock level",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.stockLevel",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read"),  # read-only projection, composed from v1+v2
    )
    # Composed here from the v1/v2 fan-in — there is no single upstream collection
    # to proxy, so ``request`` never falls through to the generic _get.
    v3_path = ""
    include = ""
    preview_template = "{{product.name}}"
    sections = {"general": {"label": "General"}}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "product": prop(
                "reference",
                "Product",
                reference="Product",
                renderProperty="name",
                **RO,
                section="general",
                filterable=True,
                previewable=True,
                description=(
                    "Anchor filter: with filter[product] the level is read per storage "
                    "location from /v1/products/{id}/storageLocations."
                ),
            ),
            "warehouse": prop(
                "reference",
                "Warehouse",
                reference="Warehouse",
                renderProperty="name",
                **RO,
                section="general",
                filterable=True,
                description=(
                    "Narrows a product- or location-anchored query; not an anchor on "
                    "its own (no upstream collection lists a whole warehouse's stock)."
                ),
            ),
            "storageLocation": prop(
                "reference",
                "Storage location",
                reference="StorageLocation",
                renderProperty="name",
                **RO,
                section="general",
                filterable=True,
                description=(
                    "Anchor filter: with filter[storageLocation] the level is read per "
                    "product from /v2/warehouses/{wh}/storageLocations/{loc}/items."
                ),
            ),
            "batch": prop(
                "reference",
                "Batch",
                reference="Batch",
                renderProperty="number",
                **RO,
                section="general",
            ),
            "quantity": prop(
                "embedded",
                "Quantity",
                **RO,
                section="general",
                properties={
                    "value": prop("decimal", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
            "reserved": prop(
                "embedded",
                "Reserved",
                **RO,
                section="general",
                properties={
                    "value": prop("decimal", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
            "available": prop(
                "embedded",
                "Available",
                **RO,
                section="general",
                properties={
                    "value": prop("decimal", "Value", **RO),
                    "unit": prop("string", "Unit", **RO),
                },
            ),
        }

    # ---- projection ------------------------------------------------------
    @staticmethod
    def level_row(
        *,
        product_id: Any,
        product_name: Any = None,
        product_number: Any = None,
        location_id: Any,
        location_name: Any = None,
        warehouse_id: Any = None,
        warehouse_name: Any = None,
        quantity: Any,
    ) -> dict[str, Any]:
        try:
            value: Any = float(quantity)
        except (TypeError, ValueError):
            value = None
        return {
            "object": "stockLevel",
            "id": (
                f"slv_{product_id}_{location_id}"
                if product_id is not None and location_id is not None
                else None
            ),
            "product": ref("prd_", product_id, product_number, product_name, "products"),
            "warehouse": ref("wh_", warehouse_id, None, warehouse_name, "warehouses"),
            "storageLocation": ref("loc_", location_id, None, location_name, "storageLocations"),
            # Batch split exists only on the v2-items path — see module docstring.
            "batch": None,
            "quantity": {"value": value, "unit": None},
            "reserved": {"value": None, "unit": None},
            "available": {"value": None, "unit": None},
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        # Rows are already projected by the fan-in; kept for base-class symmetry.
        return r

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # A projection is not writable against the facade; corrections flow
        # through stockMovement / stockTake, not here.
        return {}, {k for k in model if k != "object"}

    # ---- fan-in ----------------------------------------------------------
    async def _by_product(
        self, product_id: str, base_url: str, headers: dict[str, str], client
    ) -> tuple[int, list[dict[str, Any]]]:
        """Every storage location holding this product (v1 product sub-resource).

        The endpoint reports no total, only the echoed page — so pages are pulled
        until one comes back short."""
        url = f"{base_url.rstrip('/')}/api/v1/products/{numeric(product_id)}/storageLocations"
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            status, body = await get_json(
                url,
                [("page[number]", str(page)), ("page[size]", str(_PAGE_SIZE))],
                headers,
                client,
            )
            if status >= 400:
                return status, []
            rows = (body.get("data") if isinstance(body, dict) else None) or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                loc = row.get("storageLocation") or {}
                wh = loc.get("warehouse") or {} if isinstance(loc, dict) else {}
                prod = row.get("product") or {}
                out.append(
                    self.level_row(
                        product_id=(prod.get("id") if isinstance(prod, dict) else None)
                        or numeric(product_id),
                        location_id=loc.get("id") if isinstance(loc, dict) else None,
                        location_name=loc.get("name") if isinstance(loc, dict) else None,
                        warehouse_id=wh.get("id") if isinstance(wh, dict) else None,
                        warehouse_name=wh.get("name") if isinstance(wh, dict) else None,
                        quantity=row.get("amount"),
                    )
                )
            if len(rows) < _PAGE_SIZE:
                break
        return 200, out

    async def _by_location(
        self, location_id: str, base_url: str, headers: dict[str, str], client
    ) -> tuple[int, list[dict[str, Any]]]:
        """Every product stored on this location (v2 storage items)."""
        row = await resolve_location_row(
            location_id, base_url=base_url, headers=headers, client=client
        )
        if row is None:
            return 404, []
        wh = row.get("warehouse") or {}
        wh_id = wh.get("id") if isinstance(wh, dict) else wh
        if not wh_id:
            return 404, []
        loc_num = numeric(location_id)
        url = f"{base_url.rstrip('/')}/api/v2/warehouses/{wh_id}/storageLocations/{loc_num}/items"
        out: list[dict[str, Any]] = []
        seen = 0
        for page in range(1, _MAX_PAGES + 1):
            status, body = await get_json(
                url,
                [("page[number]", str(page)), ("page[size]", str(_PAGE_SIZE))],
                headers,
                client,
            )
            if status >= 400:
                return status, []
            rows = (body.get("data") if isinstance(body, dict) else None) or []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                out.append(
                    self.level_row(
                        product_id=item.get("productId"),
                        product_number=item.get("sku"),
                        location_id=loc_num,
                        location_name=row.get("designation"),
                        warehouse_id=wh_id,
                        warehouse_name=wh.get("name") if isinstance(wh, dict) else None,
                        quantity=item.get("quantity"),
                    )
                )
            seen += len(rows)
            total = ((body.get("extra") or {}).get("totalCount")) if isinstance(body, dict) else 0
            if not rows or seen >= int(total or 0):
                break
        return 200, out

    # ---- request ---------------------------------------------------------
    async def request(
        self, *, method, handle, query, body, base_url, token, accept_language=None, client=None
    ) -> AdapterResponse:
        method = method.upper()
        if method != "GET":
            # Let the base gate non-reads against the declared operations (405).
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
        headers = self._headers(token, accept_language)
        if handle:
            return await self._read_one(handle, base_url, headers, client)
        return await self._list(query, base_url, headers, client)

    @staticmethod
    def _matches(row: dict[str, Any], field: str, wanted: str) -> bool:
        node = row.get(field)
        got = node.get("id") if isinstance(node, dict) else None
        return got is not None and numeric(str(got)) == numeric(wanted)

    async def _list(
        self,
        query: list[tuple[str, str]],
        base_url: str,
        headers: dict[str, str],
        client: httpx.AsyncClient | None,
    ) -> AdapterResponse:
        filters = parse_filters(query)
        unsupported = [
            f"{k} (op {op})" for k, (op, _) in filters.items() if op not in ("equals", "in")
        ]
        if unsupported:
            return self._json(
                422,
                {
                    "title": "stockLevel: unsupported filter operator",
                    "detail": "This projection filters by equality only.",
                    "filters": unsupported,
                },
            )
        product = filters.get("product", (None, None))[1]
        location = filters.get("storageLocation", (None, None))[1]
        warehouse = filters.get("warehouse", (None, None))[1]

        if product:
            status, rows = await self._by_product(product, base_url, headers, client)
        elif location:
            status, rows = await self._by_location(location, base_url, headers, client)
        else:
            return self._json(
                422,
                {
                    "title": "stockLevel: an anchor filter is required",
                    "detail": (
                        "Upstream has no global stock collection; a level is readable "
                        "only per product or per storage location. Send "
                        "filter[product] or filter[storageLocation] "
                        "(warehouse/batch narrow a result, they cannot anchor it)."
                    ),
                    "anchors": ["product", "storageLocation"],
                },
            )
        if status >= 400:
            return self._json(
                status,
                {
                    "title": "stockLevel: upstream read failed",
                    "detail": f"anchor {'product' if product else 'storageLocation'} not readable",
                },
            )

        if location and product:
            rows = [r for r in rows if self._matches(r, "storageLocation", location)]
        if warehouse:
            rows = [r for r in rows if self._matches(r, "warehouse", warehouse)]
        if filters.get("batch"):
            return self._json(
                422,
                {
                    "title": "stockLevel: batch is not a filterable grain",
                    "detail": (
                        "This projection reports product × storage location. The batch "
                        "split is a blue wish (erp-spec.yaml)."
                    ),
                },
            )

        page, per_page = self._query_paging(query)
        total = len(rows)
        window = rows[(page - 1) * per_page : (page - 1) * per_page + per_page]
        payload = {"extra": {"totalCount": total, "page": {"number": page, "size": per_page}}}
        return self._json(200, self._list_envelope(window, payload, query))

    async def _read_one(
        self,
        handle: str,
        base_url: str,
        headers: dict[str, str],
        client: httpx.AsyncClient | None,
    ) -> AdapterResponse:
        """``slv_<product>_<location>`` — the composite key the projection emits."""
        parts = handle.split("_")
        if len(parts) != 3 or parts[0] != "slv" or not (parts[1] and parts[2]):
            return self._json(
                422,
                {
                    "title": f"stockLevel: malformed id {handle!r}",
                    "detail": "Expected slv_<productId>_<storageLocationId>.",
                },
            )
        status, rows = await self._by_product(parts[1], base_url, headers, client)
        if status >= 400:
            return self._json(status, {"title": f"stockLevel {handle}: product not readable"})
        for row in rows:
            if row.get("id") == handle:
                return self._json(200, {"data": row})
        return self._json(404, {"title": f"stockLevel {handle} not found"})
