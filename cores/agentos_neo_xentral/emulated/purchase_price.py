"""Xentral V3 facade · purchasePrice — supplier purchase prices incl. scale tiers.

The purchase side of pricing: a supplier's price for a product from a minimum
quantity (Einkaufs-Staffelpreis). The upstream has no v3 resource — v2 purchasePrices
is the current generation (v1 is deprecated) — so this facade reads and writes v2:

  * read   ``GET /api/v2/purchasePrices`` (list). v2 has no single-record GET, so a
    ``get``/read-back is a list filtered by ``id`` (``_get`` override below).
  * create ``POST /api/v2/purchasePrices`` — required product + fromQuantity + price;
    answers 201 with an EMPTY body and the new id in the Location header.
  * update ``PATCH /api/v2/purchasePrices/{id}`` (v2; product is fixed → not in body).
  * delete ``DELETE /api/v1/purchasePrices/{id}`` (no v2 delete; v1 delete is live).

Each record is ONE price row (one quantity tier for one supplier), so a real scale
price is several entries differing only in ``minQuantity`` (upstream ``fromQuantity``).

The v2 list requires ``page[number]`` + ``page[size]`` (size 10..50) — ``v1_paging``
guarantees them. The price amount is a STRING on create but a NUMBER on the v2 PATCH
schema — ``map_write`` formats it per operation.
"""

from __future__ import annotations

from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, money, prop, ref

_CU: dict[str, Any] = {"creatable": True, "updatable": True}
_C: dict[str, Any] = {"creatable": True}

# v2 is the current purchasePrices generation (v1 create/update are deprecated).
# There is no v2 delete, so delete falls to the (non-deprecated) v1 delete.
_PP_V2 = "/api/v2/purchasePrices"
_PP_V1 = "/api/v1/purchasePrices"


class PurchasePriceAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="PurchasePrice",
        label_en="Purchase price",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.purchasePrice",
        source_apis=("agentos_neo_xentral",),
        # One record = one supplier price row (one quantity tier). Full CRUD on v2
        # purchasePrices (delete on v1) — see _send / _get.
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = _PP_V2  # read list (and the base's PATCH target via write_path)
    write_path = _PP_V2
    money_pairs = ("unitPrice",)
    include = ""
    preview_template = "{{product.name}}"
    v1_paging = True
    sections = {
        "general": {"label": "General"},
        "supplier": {"label": "Supplier"},
        "price": {"label": "Price"},
    }

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            # product fixes the row's identity → create-only (v2 PATCH has no product).
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
            "supplier": prop(
                "reference",
                "Supplier",
                **_CU,
                reference="Supplier",
                renderProperty="name",
                section="supplier",
                filterable=True,
            ),
            "isStandardSupplier": prop("boolean", "Standard supplier", **_CU, section="supplier"),
            "supplierDesignation": prop(
                "string", "Supplier designation", **_CU, section="supplier"
            ),
            "supplierItemNumber": prop("string", "Supplier item number", **_CU, section="supplier"),
            # the quantity tier (Staffel threshold) + price/validity are editable.
            "minQuantity": prop(
                "decimal", "Min quantity", **_CU, section="price", previewable=True
            ),
            "packageAmount": prop("decimal", "Package amount", **_CU, section="price"),
            "unitPrice": prop(
                "embedded",
                "Unit price",
                section="price",
                properties={
                    "amount": prop("string", "Amount", **_CU),
                    "currency": prop("string", "Currency", **_CU),
                },
            ),
            "validFrom": prop("date", "Valid from", **_CU, section="price"),
            "validUntil": prop("date", "Valid until", **_CU, section="price"),
            "remark": prop("string", "Remark", **_CU, section="general"),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        prod = r.get("product") or {}
        sup = r.get("supplier") or {}
        price = r.get("price") or {}
        m = money(price.get("amount"), price.get("currency") or "EUR") or {}
        return {
            "object": "purchasePrice",
            "id": (f"pp_{r.get('id')}" if r.get("id") is not None else None),
            "product": ref(
                "prd_", prod.get("id") if isinstance(prod, dict) else prod, None, None, "products"
            ),
            "supplier": ref(
                "sup_", sup.get("id") if isinstance(sup, dict) else sup, None, None, "suppliers"
            ),
            "isStandardSupplier": r.get("isStandardSupplier"),
            "supplierDesignation": r.get("supplierDesignation") or None,
            "supplierItemNumber": r.get("supplierItemNumber") or None,
            "minQuantity": r.get("fromQuantity"),
            "packageAmount": r.get("packageAmount"),
            "unitPrice": {"amount": m.get("amount"), "currency": m.get("currency")},
            "validFrom": r.get("validFrom"),
            "validUntil": r.get("expiresAt"),
            "remark": r.get("internalComment") or None,
            "createdAt": None,
            "updatedAt": None,
        }

    # ---- write mapping ---------------------------------------------------
    _WRITABLE = {
        "product",
        "supplier",
        "isStandardSupplier",
        "supplierDesignation",
        "supplierItemNumber",
        "minQuantity",
        "packageAmount",
        "unitPrice",
        "validFrom",
        "validUntil",
        "remark",
    }
    _IGNORE = {"object", "id", "createdAt", "updatedAt"}

    @staticmethod
    def _ref_id(value: Any) -> str | None:
        """A model reference ({id: "sup_7"} or a bare id) → the bare numeric upstream
        id (speaking prefix stripped, ADR-002). None clears it."""
        ident = value.get("id") if isinstance(value, dict) else value
        if ident in (None, ""):
            return None
        ident = str(ident)
        return ident.split("_", 1)[1] if "_" in ident else ident

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """Map the model onto the v2 purchasePrices body. ``product`` fixes the row →
        sent only on create (the v2 PATCH schema has no product). The price amount is
        a STRING on create but a NUMBER on the v2 PATCH schema. Unknown keys 409."""
        body: dict[str, Any] = {}
        rejected: set[str] = set()

        if creating:
            pid = self._ref_id(model.get("product")) if "product" in model else None
            if pid is not None:
                body["product"] = {"id": pid}

        if model.get("supplier") is not None:
            sid = self._ref_id(model["supplier"])
            if sid is not None:
                body["supplier"] = {"id": sid}
        if model.get("isStandardSupplier") is not None:
            body["isStandardSupplier"] = bool(model["isStandardSupplier"])
        if model.get("supplierDesignation") is not None:
            body["supplierDesignation"] = model["supplierDesignation"]
        if model.get("supplierItemNumber") is not None:
            body["supplierItemNumber"] = model["supplierItemNumber"]

        # quantity tier — required by v2 create; default to the base tier (1).
        mq = model.get("minQuantity")
        if mq is not None:
            body["fromQuantity"] = mq
        elif creating:
            body["fromQuantity"] = 1

        if model.get("packageAmount") is not None:
            body["packageAmount"] = model["packageAmount"]

        up = model.get("unitPrice") or {}
        if isinstance(up, dict) and up.get("amount") is not None:
            amount = up["amount"]
            try:
                # create wants a string amount; v2 PATCH wants a number.
                wire = str(amount) if creating else float(amount)
            except (TypeError, ValueError):
                # A non-numeric amount used to raise straight out of map_write and
                # take the whole request down with it. Refusing the field names it
                # for the caller instead of answering with a stack trace.
                rejected.add("unitPrice.amount")
            else:
                body["price"] = {
                    "amount": wire,
                    "currency": up.get("currency") or "EUR",
                }

        if model.get("validFrom") is not None:
            body["validFrom"] = model["validFrom"]
        if model.get("validUntil") is not None:
            body["expiresAt"] = model["validUntil"]
        if model.get("remark") is not None:
            body["internalComment"] = model["remark"]

        for k in model:
            if k not in self._WRITABLE and k not in self._IGNORE:
                rejected.add(k)
        return body, rejected

    # ---- read: no v2 single GET → read one via list filtered by id -------
    async def _get(self, base_url, token, *, handle, query, accept_language, client):  # noqa: ANN001
        if not handle:
            return await super()._get(
                base_url,
                token,
                handle=None,
                query=query,
                accept_language=accept_language,
                client=client,
            )
        up = handle.split("_", 1)[1] if "_" in handle else handle
        q = [
            ("filter[0][key]", "id"),
            ("filter[0][op]", "equals"),
            ("filter[0][value]", str(up)),
            ("page[number]", "1"),
            ("page[size]", "10"),
        ]
        status, payload = await super()._get(
            base_url,
            token,
            handle=None,
            query=q,
            accept_language=accept_language,
            client=client,
        )
        if status >= 400:
            return status, payload
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        if rows:
            return 200, {"data": rows[0]}
        return 404, {"title": f"{self.manifest.key} {handle} not found"}

    # ---- write dispatch --------------------------------------------------
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
        """v2 create answers empty + a Location header — surface the new id as a
        synthetic ``{data:{id}}`` so the base write flow can read it back."""
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
        """POST → v2 (Location-header id). DELETE → v1 (no v2 delete). PATCH/PUT →
        base (v2 write_path /{id})."""
        method = method.upper()
        root = base_url.rstrip("/")
        if method == "POST":
            resp = await self._http(
                "POST", f"{root}{_PP_V2}", token, accept_language, client, payload=payload
            )
            if resp.status_code < 400:
                return resp.status_code, self._created_body(resp)
            return resp.status_code, self._safe_json(resp)
        if method == "DELETE":
            resp = await self._http(
                "DELETE", f"{root}{_PP_V1}/{up_handle}", token, accept_language, client
            )
            return resp.status_code, self._safe_json(resp)
        return await super()._send(
            base_url, token, method, up_handle, payload, accept_language, client
        )
