"""Xentral V3 facade · priceList — Konditionen (docs/01-model.md §6.5, ADR-012).

Honest partial: the upstream has no named price-list containers — only flat sales-
price rows (``GET /v1/salesPrices``: product × customer/customerGroup × minQuantity
→ price, with validity). This facade exposes each row as a price ENTRY, so a full
scale price (Staffelpreis) is expressed as several entries that differ only in
``minQuantity``.

Reads use v1 salesPrices (the id/customerGroup shapes are grounded there). WRITES
compose on the salesPrices resource: create prefers **v3** (``POST /api/v3/
salesPrices``, model-aligned) and falls back to **v1** (``POST /api/v1/salesPrices``)
when v3 is unavailable/not-permitted; update uses **v1** (``PATCH /api/v1/
salesPrices/{id}`` — v3 exposes only a bulk updateMultiple, no single-record PATCH);
delete prefers v3 then v1. The ONLY body difference between the versions is the
quantity-tier key: v3 calls it ``quantity`` (required), v1 calls it ``amount`` —
``map_write`` emits the canonical v3 name and ``_send`` renames it for the v1 paths.

The v1 endpoints REQUIRE both ``page[number]`` and ``page[size]`` with size 10..50
— ``_get`` guarantees them (clamping the gateway's requested size).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, money, prop, ref

# create+update vs create-only field-flag shorthands (mirror product.py's _CU/_C).
_CU: dict[str, Any] = {"creatable": True, "updatable": True}
_C: dict[str, Any] = {"creatable": True}

# salesPrices write paths. v3 is model-aligned (Beta); v1 is the stable full-CRUD
# fallback. The quantity-tier key differs: v3 ``quantity`` vs v1 ``amount``.
_SP_V3 = "/api/v3/salesPrices"
_SP_V1 = "/api/v1/salesPrices"
# Only fall back to v1 when v3 looks unavailable/not-permitted (not deployed,
# missing scope, method not allowed). A real validation answer surfaces as-is.
_SP_FALLBACK_STATUSES = frozenset({403, 404, 405, 501})
# Customer/price groups are read-only via the API (GET /api/v1/groups; POST 404s).
# Used to validate scope.customerGroup before a write — see _group_exists.
_GROUPS_PATH = "/api/v1/groups"


class PriceListAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="PriceList",
        label_en="Price list",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.priceList",
        source_apis=("agentos_neo_xentral",),
        description=(
            "Customer-specific, customer-group and scale/tiered (Staffel) sales "
            "prices, each with its own quantity threshold and validity. Use this "
            "for any price that is scoped to a customer or group, or tiered by "
            "quantity. The plain standard list price is set on the Product entity "
            "(prices.sale)."
        ),
        # Each record is one sales-price row (one tier). Write composes on the
        # salesPrices resource (v3 primary, v1 fallback — see _send).
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/v1/salesPrices"
    include = ""
    preview_template = "{{product.name}}"
    query_aliases = {"product": "productId"}
    v1_paging = True
    sections = {
        "general": {"label": "General"},
        "scope": {"label": "Scope"},
        "price": {"label": "Price"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "deactivate",
                        "Deactivate",
                        wish="v1 salesPrices carries no activation state write.",
                    ),
                    self.step_cmd(
                        "activate",
                        "Activate",
                        wish="v1 salesPrices carries no activation state write.",
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "duplicate",
                "Duplicate (validFrom)",
                wish="A duplicate-with-validFrom composer is not built (create a new entry instead).",
            ),
            self.action_def(
                "bulkAdjust",
                "Bulk adjust (percent)",
                wish="A percentage bulk adjustment has no endpoint and is not composed.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "currency": prop("string", "Currency", **RO, section="general"),
            "entries": prop(
                "collection",
                "Entries",
                **RO,
                section="general",
                node={
                    "properties": {
                        "product": prop("reference", "Product", **RO, reference="Product"),
                        "unitPrice": prop("string", "Unit price", **RO),
                        "minQuantity": prop("number", "Min quantity", **RO),
                    }
                },
            ),
            # product + scope define the row's identity → create-only (a v1 PATCH
            # cannot move a price to another product; change = new entry).
            "product": prop(
                "reference",
                "Product",
                **_C,
                reference="Product",
                renderProperty="name",
                section="general",
                filterable=True,
                previewable=True,
            ),
            "scope": prop(
                "embedded",
                "Scope",
                section="scope",
                properties={
                    "customer": prop(
                        "reference", "Customer", **_C, reference="Customer", renderProperty="name"
                    ),
                    "customerGroup": prop("string", "Customer group", **_C),
                },
            ),
            # The quantity tier (Staffel threshold) and the price/validity are the
            # editable facets of an entry.
            "minQuantity": prop(
                "decimal", "Min quantity", **_CU, section="price", previewable=True
            ),
            "unitPrice": prop(
                "embedded",
                "Unit price",
                section="price",
                properties={
                    "amount": prop("string", "Amount", **_CU),
                    "currency": prop("string", "Currency", **_CU),
                    "amountGross": prop("string", "Gross amount", **RO),
                    "taxRate": prop("string", "Tax rate", **RO),
                },
            ),
            "validFrom": prop("date", "Valid from", **_CU, section="price"),
            "validUntil": prop("date", "Valid until", **_CU, section="price"),
            "remark": prop("string", "Remark", **_CU, section="general"),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        p = r.get("product") or {}
        cust = r.get("customer")
        group = r.get("customerGroup")
        price = r.get("price") or {}
        tax = price.get("taxRate") or {}
        m = money(price.get("amount"), price.get("currency") or "EUR") or {}
        mg = money(price.get("amountGross"), price.get("currency") or "EUR") or {}
        return {
            "object": "priceListEntry",
            "currency": r.get("currency"),
            "entries": None,
            "id": (f"ple_{r.get('id')}" if r.get("id") is not None else None),
            "product": ref(
                "prd_", p.get("id") if isinstance(p, dict) else p, None, None, "products"
            ),
            "scope": {
                "customer": ref(
                    "cus_",
                    cust.get("id") if isinstance(cust, dict) else cust,
                    None,
                    None,
                    "customers",
                ),
                # v1 delivers customerGroup as a {id} reference (nullable); expose the
                # bare id string the field type promises.
                "customerGroup": (group.get("id") if isinstance(group, dict) else group),
            },
            "minQuantity": r.get("amount"),
            "unitPrice": {
                "amount": m.get("amount"),
                "currency": m.get("currency"),
                "amountGross": mg.get("amount"),
                "taxRate": tax.get("type"),
            },
            "validFrom": r.get("validFrom"),
            "validUntil": r.get("expiresAt"),
            "remark": r.get("remark") or None,
            "createdAt": None,
            "updatedAt": None,
        }

    # ---- write mapping ---------------------------------------------------
    # Top-level model keys mapped onto the salesPrices body.
    _WRITABLE = {
        "product",
        "scope",
        "minQuantity",
        "unitPrice",
        "validFrom",
        "validUntil",
        "remark",
    }
    # Keys the read emits that are system/computed — accepted silently, never sent.
    _IGNORE = {"object", "id", "currency", "entries", "createdAt", "updatedAt"}

    @staticmethod
    def _ref_id(value: Any) -> str | None:
        """A model reference ({id: "cus_7"} or a bare id) → the bare numeric upstream
        id (speaking prefix stripped, ADR-002). None clears it."""
        ident = value.get("id") if isinstance(value, dict) else value
        if ident in (None, ""):
            return None
        ident = str(ident)
        return ident.split("_", 1)[1] if "_" in ident else ident

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """Map the model onto the CANONICAL v3 salesPrices body (``quantity`` tier
        name); ``_send`` renames it to ``amount`` for the v1 paths. product + scope
        are the entry's identity → sent only on create. Unknown top-level keys 409."""
        body: dict[str, Any] = {}
        rejected: set[str] = set()

        # product + scope define the row → create-only (v1 PATCH cannot change them).
        if creating:
            pid = self._ref_id(model.get("product")) if "product" in model else None
            if pid is not None:
                body["product"] = {"id": pid}
            scope = model.get("scope") or {}
            if isinstance(scope, dict):
                cid = self._ref_id(scope.get("customer"))
                if cid is not None:
                    body["customer"] = {"id": cid}
                gid = self._ref_id(scope.get("customerGroup"))
                if gid is not None:
                    body["customerGroup"] = {"id": gid}

        # quantity tier — v3 requires it on create; default to the base tier (1).
        mq = model.get("minQuantity")
        if mq is not None:
            body["quantity"] = mq
        elif creating:
            body["quantity"] = 1

        # price
        up = model.get("unitPrice") or {}
        if isinstance(up, dict) and up.get("amount") is not None:
            body["price"] = {
                "amount": str(up["amount"]),
                "currency": up.get("currency") or "EUR",
            }

        # validity + remark
        if model.get("validFrom") is not None:
            body["validFrom"] = model["validFrom"]
        if model.get("validUntil") is not None:
            body["expiresAt"] = model["validUntil"]
        if model.get("remark") is not None:
            body["remark"] = model["remark"]

        for k in model:
            if k not in self._WRITABLE and k not in self._IGNORE:
                rejected.add(k)
        return body, rejected

    # ---- write dispatch (v3 primary, v1 fallback) ------------------------
    @staticmethod
    def _to_v1(body: dict[str, Any], *, for_update: bool = False) -> dict[str, Any]:
        """Canonical (v3) body → v1 body: the quantity tier is ``amount`` on v1, and
        v1's PATCH has no ``product`` field (identity is fixed on the row)."""
        v1 = dict(body)
        if "quantity" in v1:
            v1["amount"] = v1.pop("quantity")
        if for_update:
            v1.pop("product", None)
        return v1

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return {}

    async def _http(
        self,
        method: str,
        url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.request(method, url, json=payload, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                return await _do(c)
        return await _do(client)

    def _created_body(self, resp: httpx.Response) -> dict[str, Any]:
        """Normalize a create response to ``{data:{id}}``. v3 returns the record in
        the body; v1 returns an empty body with the new id in the Location header."""
        body = self._safe_json(resp)
        rid = (body.get("data") or {}).get("id") if isinstance(body, dict) else None
        if not rid:
            loc = resp.headers.get("Location") or resp.headers.get("location")
            if loc:
                rid = loc.rstrip("/").rsplit("/", 1)[-1] or None
        if rid:
            return {"data": {"id": rid}}
        return body if isinstance(body, dict) else {}

    async def _send(  # noqa: ANN001
        self, base_url, token, method, up_handle, payload, accept_language, client
    ):
        """salesPrices dispatch. POST: v3 then v1 fallback. PATCH/PUT: v1 /{id} (v3
        has no single-record update). DELETE: v3 /{id} then v1 fallback."""
        method = method.upper()
        root = base_url.rstrip("/")
        if method == "POST":
            resp = await self._http(
                "POST", f"{root}{_SP_V3}", token, accept_language, client, payload=payload
            )
            if resp.status_code >= 400 and resp.status_code in _SP_FALLBACK_STATUSES:
                resp = await self._http(
                    "POST",
                    f"{root}{_SP_V1}",
                    token,
                    accept_language,
                    client,
                    payload=self._to_v1(payload),
                )
            if resp.status_code < 400:
                return resp.status_code, self._created_body(resp)
            return resp.status_code, self._safe_json(resp)
        if method in ("PATCH", "PUT"):
            # v3 salesPrices exposes only a bulk updateMultiple; a single-record
            # update goes to v1 PATCH /{id}.
            resp = await self._http(
                "PATCH",
                f"{root}{_SP_V1}/{up_handle}",
                token,
                accept_language,
                client,
                payload=self._to_v1(payload, for_update=True),
            )
            return resp.status_code, self._safe_json(resp)
        if method == "DELETE":
            resp = await self._http(
                "DELETE", f"{root}{_SP_V3}/{up_handle}", token, accept_language, client
            )
            if resp.status_code >= 400 and resp.status_code in _SP_FALLBACK_STATUSES:
                resp = await self._http(
                    "DELETE", f"{root}{_SP_V1}/{up_handle}", token, accept_language, client
                )
            return resp.status_code, self._safe_json(resp)
        return await super()._send(
            base_url, token, method, up_handle, payload, accept_language, client
        )

    # ---- customer-group guard -------------------------------------------
    async def _group_exists(  # noqa: ANN001
        self, gid, base_url, token, accept_language, client
    ) -> bool:
        """Whether a customer/price group with ``gid`` exists (GET /api/v1/groups).
        Fail-OPEN: returns True whenever the listing can't be read (transient error,
        non-200, or a tenant that surfaces no groups at all), so the guard only ever
        rejects on a POSITIVE 'not found' and never blocks a price write on a flaky
        read. Groups are small; a few pages cover any SMB tenant."""
        root = base_url.rstrip("/")
        target = str(gid)
        saw_rows = False
        for page in range(1, 6):
            url = f"{root}{_GROUPS_PATH}?page[number]={page}&page[size]=100"
            try:
                resp = await self._http("GET", url, token, accept_language, client)
            except httpx.HTTPError:
                return True  # fail-open
            if resp.status_code >= 400:
                return True  # fail-open
            body = self._safe_json(resp)
            rows = body.get("data") if isinstance(body, dict) else body
            if not rows:
                break
            saw_rows = True
            for r in rows:
                if isinstance(r, dict) and str(r.get("id")) == target:
                    return True
            if len(rows) < 100:
                break
        # Read groups successfully and none matched → genuinely unknown (reject).
        # Never saw any group at all → fail-open rather than block on thin data.
        return not saw_rows

    async def _write(  # noqa: ANN001
        self, method, handle, query, body, base_url, token, accept_language, client
    ):
        """Reject a price scoped to a non-existent customer group before writing.
        Upstream salesPrices does NOT validate the group reference: it will happily
        store a price against a ghost id (e.g. ``"1"`` when no group 1 exists),
        leaving an orphaned row that binds to nothing. Validate the id against
        GET /api/v1/groups first. Runs on dryRun too, so ``bulk_validate`` catches
        it. Customer refs are left to upstream, which does validate them."""
        if method.upper() == "POST":
            try:
                model = json.loads(body or b"{}")
            except (ValueError, TypeError):
                model = {}
            scope = model.get("scope") if isinstance(model, dict) else None
            gid = (
                self._ref_id((scope or {}).get("customerGroup"))
                if isinstance(scope, dict)
                else None
            )
            if gid is not None and not await self._group_exists(
                gid, base_url, token, accept_language, client
            ):
                return self._json(
                    400,
                    {
                        "title": f"Unknown customer group id {gid!r}",
                        "detail": (
                            "scope.customerGroup must be an existing group id from "
                            "GET /api/v1/groups. Upstream salesPrices would otherwise "
                            "store this price against a non-existent group (orphaned row)."
                        ),
                    },
                )
        return await super()._write(
            method, handle, query, body, base_url, token, accept_language, client
        )
