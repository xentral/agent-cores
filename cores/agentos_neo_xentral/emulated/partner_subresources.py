"""Contact persons + addresses as partner collections (docs/01-model.md §6.1).

The model integrates them INTO the partner instead of standing up extra
entities: ``contacts`` and ``addresses`` are collections on customer/supplier,
and ``addresses`` unifies the two upstream stores under one concept with a
``type`` role (``billing`` | ``shipping``) — the same address field names the
documents already use (name/street/zip/city/country/email/phone).

Write semantics follow the tags precedent: a PATCH carries the FULL desired
set. Entries without an ``id`` are created, entries with an ``id`` are updated,
and upstream entries missing from the list are deleted.

Upstream (live, no store — ADR-014):
  contacts             v3 ``{partner}/{id}/contactPersons``      (full CRUD)
  addresses shipping   v3 ``{partner}/{id}/deliveryAddresses``   (full CRUD)
  addresses billing    v3 ``deviatingBillingAddress`` ON the partner record —
                       a SINGLETON (fixed model id "adr_billing"), both partners;
                       PATCH the record with the object (or null to clear)

Speaking ids encode the upstream store: ``con_<id>``, ``adr_s<id>`` (shipping),
``adr_billing`` (the billing singleton) — reversible without lookup (ADR-002).

The billing row lives IN the v3 partner payload, so map_read surfaces it on
EVERY row (lists included); the shipping rows need the sub-resource call and are
appended on composed reads (detail + tiny lists).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

_TIMEOUT = 25.0

# Compose sub-resources into LIST rows only for tiny pages (the Steckbrief's
# one-row sample, small previews) — beyond that the N+1 upstream cost is not
# worth it and the collections stay null (detail reads always compose).
_COMPOSE_LIST_LIMIT = 3


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def contact_from_v3(c: dict[str, Any]) -> dict[str, Any]:
    d = c.get("contactPersonDetails") or {}
    return {
        "id": f"con_{c.get('id')}" if c.get("id") is not None else None,
        "type": c.get("type"),
        "name": c.get("name"),
        "title": c.get("title"),
        "salutation": c.get("salutation"),
        # Upstream keeps these three apart; the model used to fold department into a
        # single "role" and drop position entirely.
        "position": d.get("position"),
        "department": c.get("department"),
        "subDepartment": c.get("subDepartment"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "mobile": c.get("mobile"),
        "fax": c.get("fax"),
        "language": d.get("language"),
        "birthday": d.get("birthday"),
        "allowMarketingEmails": d.get("allowMarketingEmails"),
        # The printed remark and the internal-only one are separate upstream.
        "remarks": d.get("remarks"),
        "internalNote": d.get("internalNote"),
    }


# Model key → its key inside the v3 ``contactPersonDetails`` sub-object.
_CONTACT_DETAILS = {
    "position": "position",
    "language": "language",
    "birthday": "birthday",
    "allowMarketingEmails": "allowMarketingEmails",
    "remarks": "remarks",
    "internalNote": "internalNote",
}


def contact_to_v3(m: dict[str, Any]) -> dict[str, Any]:
    out = _clean(
        {
            "type": m.get("type"),
            "name": m.get("name"),
            "title": m.get("title"),
            "salutation": m.get("salutation"),
            "department": m.get("department"),
            "subDepartment": m.get("subDepartment"),
            "email": m.get("email"),
            "phone": m.get("phone"),
            "mobile": m.get("mobile"),
            "fax": m.get("fax"),
        }
    )
    details = _clean({wire: m.get(key) for key, wire in _CONTACT_DETAILS.items()})
    if details:
        out["contactPersonDetails"] = details
    return out


def ship_addr_from_v3(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"adr_s{a.get('id')}" if a.get("id") is not None else None,
        "type": "shipping",
        "label": None,
        "isDefault": False,
        "name": a.get("name"),
        "contactPerson": a.get("contactPerson"),
        "street": a.get("street"),
        "zip": a.get("zipCode"),
        "city": a.get("city"),
        "state": a.get("state"),
        "country": a.get("country"),
        "gln": a.get("gln"),
        "email": a.get("email"),
        "phone": a.get("phone"),
    }


# The v3 deliveryAddresses endpoint requires `state` (bundesstaat) as a 2-letter
# uppercase code (e.g. "BY"), unlike primaryAddress which accepts full names. A
# full name like "Bayern" fails validation and rejects the WHOLE address, so map
# the German state names, pass a valid code through, and drop anything unknown
# (state is optional — better a stateless address than a lost one).
_DE_STATE_CODES = {
    "baden-württemberg": "BW",
    "baden-wuerttemberg": "BW",
    "bayern": "BY",
    "bavaria": "BY",
    "berlin": "BE",
    "brandenburg": "BB",
    "bremen": "HB",
    "hamburg": "HH",
    "hessen": "HE",
    "hesse": "HE",
    "mecklenburg-vorpommern": "MV",
    "niedersachsen": "NI",
    "lower saxony": "NI",
    "nordrhein-westfalen": "NW",
    "north rhine-westphalia": "NW",
    "rheinland-pfalz": "RP",
    "saarland": "SL",
    "sachsen": "SN",
    "saxony": "SN",
    "sachsen-anhalt": "ST",
    "schleswig-holstein": "SH",
    "thüringen": "TH",
    "thueringen": "TH",
    "thuringia": "TH",
}


def _delivery_state(value: Any) -> str | None:
    """Coerce a model `state` to the 2-letter uppercase code the v3
    deliveryAddresses endpoint requires. Already-valid codes pass through; known
    German state names map to their code; anything else is dropped."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _DE_STATE_CODES.get(v.lower())


def ship_addr_to_v3(m: dict[str, Any]) -> dict[str, Any]:
    return _clean(
        {
            "name": m.get("name"),
            "contactPerson": m.get("contactPerson"),
            "street": m.get("street"),
            "zipCode": m.get("zip"),
            "city": m.get("city"),
            "state": _delivery_state(m.get("state")),
            "country": m.get("country"),
            "gln": m.get("gln"),
            "email": m.get("email"),
            "phone": m.get("phone"),
        }
    )


def bill_addr_from_dba(a: dict[str, Any]) -> dict[str, Any]:
    """v3 ``deviatingBillingAddress`` (on the partner record) → model billing row.
    A SINGLETON — the model id is the fixed "adr_billing"."""
    return {
        "id": "adr_billing",
        "type": "billing",
        "label": None,
        "isDefault": False,
        "name": a.get("name"),
        "contactPerson": a.get("contactPerson"),
        "street": a.get("street"),
        "zip": a.get("zipCode"),
        "city": a.get("city"),
        "state": a.get("state"),
        "country": a.get("country"),
        "gln": None,
        "email": a.get("email"),
        "phone": a.get("phone"),
    }


def bill_addr_to_dba(m: dict[str, Any]) -> dict[str, Any]:
    return _clean(
        {
            "name": m.get("name"),
            "contactPerson": m.get("contactPerson"),
            "street": m.get("street"),
            "zipCode": m.get("zip"),
            "city": m.get("city"),
            "state": m.get("state"),
            "country": m.get("country"),
            "email": m.get("email"),
            "phone": m.get("phone"),
        }
    )


def contacts_prop(prop, RO, CU) -> dict[str, Any]:  # noqa: N803 - schema-flag bundles
    """Schema fragment for the ``contacts`` collection (shared customer/supplier)."""
    return prop(
        "collection",
        "Contact persons",
        section="contacts",
        **CU,
        node={
            "properties": {
                "id": prop("string", "ID", **RO),
                "type": prop(
                    "select",
                    "Type",
                    **CU,
                    options=[
                        {"value": v, "label": v.capitalize()}
                        for v in ("mr", "mrs", "company", "other")
                    ],
                ),
                "name": prop("string", "Name", **CU),
                "title": prop("string", "Title", **CU),
                "salutation": prop("string", "Salutation", **CU),
                "position": prop("string", "Position", **CU),
                "department": prop("string", "Department", **CU),
                "subDepartment": prop("string", "Sub-department", **CU),
                "email": prop("string", "Email", **CU),
                "phone": prop("string", "Phone", **CU),
                "mobile": prop("string", "Mobile", **CU),
                "fax": prop("string", "Fax", **CU),
                "language": prop("string", "Language", **CU),
                "birthday": prop("date", "Birthday", **CU),
                "allowMarketingEmails": prop("boolean", "Allow marketing emails", **CU),
                "remarks": prop("text", "Remarks", **CU),
                "internalNote": prop("text", "Internal note", **CU),
            }
        },
    )


def addresses_prop(prop, RO, CU) -> dict[str, Any]:  # noqa: N803 - schema-flag bundles
    """Schema fragment for the unified ``addresses`` collection.

    ONE list: the main address is the default row (``type`` "both", ``isDefault``),
    plus optional deviating billing and shipping rows. The geo leaves carry the v3
    filter/sort/search the record supports on its main address (mapped via the
    adapter's ``query_aliases``: addresses.city → city …)."""
    q = {"filterable": True, "sortable": True, "searchable": True}
    return prop(
        "collection",
        "Addresses",
        section="address",
        **CU,
        node={
            "properties": {
                "id": prop("string", "ID", **RO),
                "type": prop(
                    "select",
                    "Type",
                    **CU,
                    options=[
                        {"value": "billing", "label": "Billing"},
                        {"value": "shipping", "label": "Shipping"},
                        {"value": "both", "label": "Both"},
                    ],
                ),
                "label": prop("string", "Label", **CU),
                "isDefault": prop("boolean", "Default", **CU),
                "name": prop("string", "Name", **CU),
                "contactPerson": prop("string", "Contact person", **CU),
                "street": prop("string", "Street", **CU, **q),
                "zip": prop("string", "Zip", **CU, **q),
                "city": prop("string", "City", **CU, **q),
                "state": prop("string", "State", **CU, **q),
                "country": prop("string", "Country", **CU, **q),
                "gln": prop("string", "GLN", **CU),
                "email": prop("string", "Email", **CU),
                "phone": prop("string", "Phone", **CU),
            }
        },
    )


class PartnerSubresourcesMixin:
    """Compose/sync ``contacts`` + ``addresses`` for customer/supplier.

    The host adapter must define ``v3_path`` (…/customers | …/suppliers).
    """

    # ---- raw HTTP against explicit sub-resource paths --------------------
    async def _sub_call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> tuple[int, Any]:
        url = f"{base_url.rstrip('/')}{path}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.request(method, url, json=body, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    def _sub_paths(self, up_id: str) -> dict[str, str]:
        return {
            "contacts": f"{self.v3_path}/{up_id}/contactPersons",
            "shipping": f"{self.v3_path}/{up_id}/deliveryAddresses",
        }

    # ---- read composition ------------------------------------------------
    async def _compose(
        self,
        rec: dict[str, Any],
        up_id: str,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> None:
        paths = self._sub_paths(up_id)
        st, pl = await self._sub_call(
            "GET", f"{paths['contacts']}?perPage=50", None, base_url, token, accept_language, client
        )
        if st == 200:
            rows = (pl.get("data") if isinstance(pl, dict) else None) or []
            rec["contacts"] = [contact_from_v3(c) for c in rows if isinstance(c, dict)]
        # billing is already on the mapped record (it rides in the v3 payload);
        # append the shipping rows from the sub-resource.
        addresses: list[dict[str, Any]] = [
            a for a in (rec.get("addresses") or []) if isinstance(a, dict)
        ]
        st, pl = await self._sub_call(
            "GET", f"{paths['shipping']}?perPage=50", None, base_url, token, accept_language, client
        )
        if st == 200:
            rows = (pl.get("data") if isinstance(pl, dict) else None) or []
            addresses += [ship_addr_from_v3(a) for a in rows if isinstance(a, dict)]
        rec["addresses"] = addresses

    # ---- write sync (full desired set, tags precedent) -------------------
    async def _sync_store(
        self,
        base_path: str,
        id_prefix: str,
        desired: list[dict[str, Any]],
        to_up,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> list[str]:
        """Diff desired entries against the upstream store. Returns error notes."""
        errors: list[str] = []
        st, pl = await self._sub_call(
            "GET", f"{base_path}?perPage=50", None, base_url, token, accept_language, client
        )
        existing = (pl.get("data") if isinstance(pl, dict) else None) or [] if st == 200 else []
        existing_ids = {str(e.get("id")) for e in existing if isinstance(e, dict)}
        keep: set[str] = set()
        for entry in desired:
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("id") or "")
            up = to_up(entry)
            if eid.startswith(id_prefix) and eid[len(id_prefix) :] in existing_ids:
                up_id = eid[len(id_prefix) :]
                keep.add(up_id)
                st, pl = await self._sub_call(
                    "PATCH", f"{base_path}/{up_id}", up, base_url, token, accept_language, client
                )
                if st >= 400:
                    errors.append(f"{eid}: PATCH {st}")
            else:
                st, pl = await self._sub_call(
                    "POST", base_path, up, base_url, token, accept_language, client
                )
                if st >= 400:
                    errors.append(f"create: POST {st} {json.dumps(pl)[:120]}")
        for up_id in existing_ids - keep:
            st, _ = await self._sub_call(
                "DELETE", f"{base_path}/{up_id}", None, base_url, token, accept_language, client
            )
            if st >= 400:
                errors.append(f"{id_prefix}{up_id}: DELETE {st}")
        return errors

    async def _sync_billing(
        self,
        up_id: str,
        desired: list[dict[str, Any]],
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> list[str]:
        """The billing address is a SINGLETON riding on the partner record
        (v3 ``deviatingBillingAddress``): one desired entry upserts it, an empty
        set clears it (PATCH null)."""
        errors: list[str] = []
        if len(desired) > 1:
            errors.append("only ONE billing address exists upstream — extra entries ignored")
        body = {"deviatingBillingAddress": bill_addr_to_dba(desired[0]) if desired else None}
        st, pl = await self._sub_call(
            "PATCH", f"{self.v3_path}/{up_id}", body, base_url, token, accept_language, client
        )
        if st >= 400:
            errors.append(f"adr_billing: {st} {json.dumps(pl)[:120]}")
        return errors

    async def _sync(
        self,
        up_id: str,
        contacts: list[dict[str, Any]] | None,
        addresses: list[dict[str, Any]] | None,
        base_url: str,
        token: str,
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> list[str]:
        paths = self._sub_paths(up_id)
        errors: list[str] = []
        if contacts is not None:
            errors += await self._sync_store(
                paths["contacts"],
                "con_",
                contacts,
                contact_to_v3,
                base_url,
                token,
                accept_language,
                client,
            )
        if addresses is not None:
            shipping = [a for a in addresses if isinstance(a, dict) and a.get("type") != "billing"]
            billing = [a for a in addresses if isinstance(a, dict) and a.get("type") == "billing"]
            errors += await self._sync_store(
                paths["shipping"],
                "adr_s",
                shipping,
                ship_addr_to_v3,
                base_url,
                token,
                accept_language,
                client,
            )
            errors += await self._sync_billing(
                up_id, billing, base_url, token, accept_language, client
            )
        return errors

    # ---- request orchestration ------------------------------------------
    async def request(  # noqa: ANN001
        self,
        *,
        method,
        handle,
        query,
        body,
        base_url,
        token,
        accept_language=None,
        client=None,
    ):
        method_u = method.upper()
        collections: dict[str, Any] = {}
        if method_u in ("POST", "PATCH", "PUT") and body:
            try:
                payload = json.loads(body)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                if "contacts" in payload:
                    collections["contacts"] = payload.pop("contacts")
                if "addresses" in payload:
                    addrs = payload.pop("addresses") or []
                    # The default/main row IS the record's primaryAddress — route it
                    # onto the record (map_write handles ``primaryAddress``); only the
                    # deviating billing + shipping rows go to the sub-resource sync.
                    main = next(
                        (
                            a
                            for a in addrs
                            if isinstance(a, dict)
                            and (
                                a.get("isDefault")
                                or a.get("type") == "both"
                                or str(a.get("id")) == "adr_main"
                            )
                        ),
                        None,
                    )
                    if isinstance(main, dict):
                        payload["primaryAddress"] = {
                            k: main.get(k)
                            for k in ("name", "street", "zip", "city", "state", "country")
                            if main.get(k) is not None
                        }
                    collections["addresses"] = [a for a in addrs if a is not main]
                body = json.dumps(payload).encode()
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
        if resp.status_code >= 400:
            return resp
        try:
            out = json.loads(resp.content or b"{}")
        except ValueError:
            return resp
        data = out.get("data")
        if method_u == "GET":
            if handle and isinstance(data, dict):
                up_id = handle.split("_", 1)[1] if "_" in handle else handle
                await self._compose(data, up_id, base_url, token, accept_language, client)
                return self._json(resp.status_code, out)
            if not handle and isinstance(data, list) and 0 < len(data) <= _COMPOSE_LIST_LIMIT:
                for rec in data:
                    if isinstance(rec, dict) and rec.get("id"):
                        rid = str(rec["id"])
                        await self._compose(
                            rec,
                            rid.split("_", 1)[1] if "_" in rid else rid,
                            base_url,
                            token,
                            accept_language,
                            client,
                        )
                return self._json(resp.status_code, out)
            return resp
        if method_u in ("POST", "PATCH", "PUT") and collections:
            rid = str((data or {}).get("id") or handle or "")
            if not rid:
                return resp
            up_id = rid.split("_", 1)[1] if "_" in rid else rid
            errors = await self._sync(
                up_id,
                collections.get("contacts"),
                collections.get("addresses"),
                base_url,
                token,
                accept_language,
                client,
            )
            # The base write mapped the record BEFORE the sync mutated it (the
            # billing singleton rides ON the record) — re-read for a fresh,
            # composed view instead of patching up the stale mapping.
            fresh = await self.request(
                method="GET",
                handle=rid,
                query=[],
                body=None,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
            )
            if fresh.status_code < 400:
                try:
                    out["data"] = json.loads(fresh.content or b"{}").get("data") or data
                except ValueError:
                    pass
            if errors:
                out["subresourceErrors"] = errors
            return self._json(resp.status_code, out)
        return resp
