"""Xentral V3 facade · customer — Stammdaten (docs/01-model.md §6.1).

Reads Xentral v3 ``/api/v3/customers`` (contact fields live on ``primaryAddress``).
Master data, so no document skeleton. ``finance`` (openAmount/overdue) is computed
and not surfaced by the read API → best-effort/blue. Per ADR-014 only
upstream-writable fields are creatable/updatable; the rest are blue wishes.
"""

from __future__ import annotations

from typing import Any

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import (
    REQUIRED,
    _TIMEOUT,
    RO,
    FacadeAdapterBase,
    custom_fields_to_v3,
    map_tags,
    money,
    prop,
    ref,
    tags_prop,
    tags_to_v3,
)
from .partner_subresources import (
    PartnerSubresourcesMixin,
    addresses_from_include,
    addresses_prop,
    bill_addr_from_dba,
    contacts_from_include,
    contacts_prop,
)

_CU = {"creatable": True, "updatable": True}


class CustomerAdapter(PartnerSubresourcesMixin, FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Customer",
        label_en="Customer",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.customer",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/v3/customers"
    # This collection parses createdAt/updatedAt filters as DATES and rejects the
    # full timestamp it returns on read (400 "not a valid date") — the document
    # collections do the exact opposite. Measured on mvp; reported upstream.
    datetime_filters_take_date_only = True
    # v3 has no customer delete; v1 does (verified on mvp: 204). Without it an agent
    # can create a record but not clean up after itself — the asymmetry leaves test
    # data behind in a live tenant and makes people wary of trying anything.
    _V1_PATH = "/api/v1/customers"
    # customFields only reach the payload when asked for — without the include the
    # free-field values are simply absent, not empty.
    include = "tags,customFields,contactPersons,deliveryAddresses"
    preview_template = "{{name}}"
    # The v3 address filters/sorts act on the record's main address; in the unified
    # model that is the default row of the ``addresses`` list, so the query keys map
    # from ``addresses.*`` to the flat v3 wire keys.
    query_aliases = {
        "addresses.zip": "zipCode",
        "addresses.country": "country",
        "addresses.city": "city",
        "addresses.street": "street",
        "addresses.state": "state",
    }
    sections = {
        "general": {"label": "General"},
        "address": {"label": "Address"},
        "contacts": {"label": "Contact persons"},
        "defaults": {"label": "Defaults"},
        "finance": {"label": "Finance"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "archive", "Archive", wish="No archive flag is writable via v3 customers."
                    ),
                    self.step_cmd(
                        "reactivate",
                        "Reactivate",
                        wish="No archive flag is writable via v3 customers.",
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "setHold",
                "Set hold",
                wish="Customer holds (delivery/invoice block) are not writable via the public API.",
            ),
            self.action_def(
                "releaseHold",
                "Release hold",
                wish="Customer holds are not writable via the public API.",
            ),
            self.action_def(
                "mergeInto", "Merge into", wish="Duplicate merge is a UI-only feature — no API."
            ),
            self.action_def(
                "runCreditCheck", "Run credit check", wish="Credit checks have no public trigger."
            ),
            self.action_def(
                "statement",
                "Open-items statement",
                wish="An open-items statement is not exposed via the public API.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            # Creatable, not updatable: v3 takes a number on POST (verified on mvp —
            # a supplied number is stored verbatim) and draws one from the number
            # range when it is omitted. That makes it the migration key, and the one
            # filterable field an import can match on to stay repeatable.
            "number": prop(
                "string",
                "Number",
                creatable=True,
                section="general",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
            ),
            "type": prop("select", "Type", **RO, section="general"),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=[{"value": v, "label": v.capitalize()} for v in ("active", "archived")],
            ),
            "parent": prop(
                "reference", "Parent (HQ)", **RO, section="general", reference="Customer"
            ),
            "billTo": prop(
                "reference",
                "Bill-to (central billing)",
                **RO,
                section="general",
                reference="Customer",
            ),
            "channels": prop(
                "collection",
                "Channel links",
                **RO,
                section="general",
                node={
                    "properties": {
                        "channel": prop("reference", "Channel", **RO, reference="Channel"),
                        "externalId": prop("string", "External id", **RO),
                    }
                },
            ),
            "name": prop(
                "string",
                "Name",
                **REQUIRED,
                section="general",
                **_CU,
                filterable=True,
                sortable=True,
                searchable=True,
                previewable=True,
            ),
            "email": prop(
                "string",
                "Email",
                section="general",
                **_CU,
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "phone": prop("string", "Phone", section="general", **_CU),
            "website": prop("string", "Website", section="general", **_CU),
            # vatId is not a query field on v3 /customers (no filter/sort/search) —
            # writable only.
            "vatId": prop("string", "VAT id", section="general", **_CU),
            "language": prop("string", "Language", section="general", **_CU),
            # No separate primaryAddress block — the main address is the default row
            # (type "both", isDefault) of the unified addresses list.
            "addresses": addresses_prop(prop, RO, _CU),
            "contacts": contacts_prop(prop, RO, _CU, REQUIRED),
            "defaults": prop(
                "embedded",
                "Defaults",
                section="defaults",
                properties={
                    "currency": prop("string", "Currency"),
                    "language": prop("string", "Language"),
                    "paymentMethod": prop(
                        "reference",
                        "Payment method",
                        reference="PaymentMethod",
                        renderProperty="name",
                    ),
                    "paymentTerms": prop(
                        "embedded",
                        "Payment terms",
                        properties={
                            "dueDays": prop("integer", "Due days"),
                            "discountPercent": prop("decimal", "Discount %"),
                            "discountDays": prop("integer", "Discount days"),
                        },
                    ),
                    "taxation": prop("select", "Taxation"),
                    "shippingMethod": prop(
                        "reference", "Shipping method", **RO, reference="ShippingMethod"
                    ),
                    "priceList": prop("reference", "Price list", **RO, reference="PriceList"),
                    "partialShipping": prop("select", "Partial shipping", **RO),
                },
            ),
            "finance": prop(
                "embedded",
                "Finance",
                **RO,
                section="finance",
                properties={
                    "openAmount": prop(
                        "embedded",
                        "Open amount",
                        **RO,
                        properties={
                            "amount": prop("string", "Amount", **RO),
                            "currency": prop("string", "Currency", **RO),
                        },
                    ),
                    "creditLimit": prop(
                        "embedded",
                        "Credit limit",
                        properties={
                            "amount": prop("string", "Amount"),
                            "currency": prop("string", "Currency"),
                        },
                    ),
                    "onHold": prop("boolean", "On hold"),
                    "dunningBlocked": prop("boolean", "Dunning blocked"),
                    "debtorAccountNumber": prop("string", "Debtor account"),
                },
            ),
            "project": prop(
                "reference",
                "Main project",
                reference="Project",
                renderProperty="name",
                section="general",
                **_CU,
            ),
            "channel": prop(
                "reference",
                "Origin channel",
                reference="Channel",
                renderProperty="name",
                section="general",
                **_CU,
            ),
            "tags": tags_prop(writable=True),
            # Free-text CRM note on the partner ("Sonstiges" in the mask) — v3
            # `notes`, writable. This is where migrated CRM remarks belong.
            "notes": prop("text", "Notes", section="general", **_CU),
            # Free-field VALUES. A typed collection, not an untyped embedded blob:
            # an agent has to be able to read the payload shape off the schema.
            # v3 include=customFields returns {key, label, type, value}; the write
            # takes {key, label, value} — label is required upstream. The roster of
            # fields that exist lives on the AddressCustomField entity.
            "customFields": prop(
                "collection",
                "Custom fields",
                section="general",
                node={
                    "properties": {
                        "key": prop("string", "Key", **_CU),
                        "label": prop("string", "Label", **_CU),
                        "type": prop("string", "Type", **RO),
                        "value": prop("string", "Value", **_CU),
                    }
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, filterable=True, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, filterable=True, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        pa = r.get("primaryAddress") or {}
        comm = r.get("communication") or {}
        fin = r.get("financials") or {}
        ful = r.get("fulfillment") or {}
        terms = fin.get("paymentTerms") or {}
        mp = r.get("mainProject")
        ch = r.get("originSalesChannel")
        return {
            "object": "customer",
            "id": (f"cus_{r.get('id')}" if r.get("id") is not None else None),
            "number": r.get("number"),
            "type": pa.get("type"),
            "status": None,
            "parent": None,
            "billTo": None,
            "channels": None,
            "name": pa.get("name"),
            "email": pa.get("email"),
            "phone": pa.get("phone"),
            "website": comm.get("website"),
            "vatId": (fin.get("tax") or {}).get("vatId"),
            "language": comm.get("language"),
            # ONE unified address list: the main address (v3 primaryAddress) is the
            # default row (type "both"); the billing singleton rides in the v3 payload
            # (surfaced on EVERY row); shipping rows are appended on composed reads.
            "addresses": addresses_from_include(
                r,
                [
                    {
                        "id": "adr_main",
                        "type": "both",
                        "label": "Hauptadresse",
                        "isDefault": True,
                        "name": pa.get("name"),
                        "contactPerson": None,
                        "street": pa.get("street"),
                        "zip": pa.get("zipCode"),
                        "city": pa.get("city"),
                        "state": pa.get("state"),
                        "country": pa.get("country"),
                        "gln": None,
                        "email": pa.get("email"),
                        "phone": pa.get("phone"),
                    },
                    *(
                        [bill_addr_from_dba(r["deviatingBillingAddress"])]
                        if isinstance(r.get("deviatingBillingAddress"), dict)
                        else []
                    ),
                ],
            ),
            "contacts": contacts_from_include(r),
            "defaults": {
                "currency": fin.get("currency"),
                "language": comm.get("language"),
                "paymentMethod": ref(
                    "paym_",
                    (fin.get("paymentMethod") or {}).get("id"),
                    None,
                    None,
                    "paymentMethods",
                ),
                # v3 financials.paymentTerms — the terms an order inherits when it is
                # created without its own (see the order's payment block).
                "paymentTerms": {
                    "dueDays": terms.get("paymentTargetDays"),
                    "discountPercent": terms.get("paymentTargetDiscount"),
                    "discountDays": terms.get("paymentTargetDiscountDays"),
                },
                "taxation": (fin.get("tax") or {}).get("taxation"),
                "shippingMethod": ref(
                    "ship_",
                    (ful.get("shippingMethod") or {}).get("id"),
                    None,
                    None,
                    "shippingMethods",
                ),
                # No slot on the v3 customer resource — the price list a customer is
                # assigned to, and whether partial shipping is allowed, are not part of
                # this payload. Blue wishes rather than silent nulls.
                "priceList": None,
                "partialShipping": None,
            },
            "finance": {
                # Open receivables are a computed A/R figure, not a customer field —
                # nothing on this resource carries them.
                "openAmount": None,
                "creditLimit": money(fin.get("creditLimit"), fin.get("defaultCurrency")),
                "onHold": ful.get("deliveryBlock"),
                "dunningBlocked": None,
                "debtorAccountNumber": r.get("deviatingDebtorAccountNumber"),
            },
            "project": ref(
                "prj_", mp.get("id") if isinstance(mp, dict) else mp, None, None, "projects"
            ),
            "channel": ref(
                "ch_", ch.get("id") if isinstance(ch, dict) else ch, None, None, "channels"
            ),
            "tags": map_tags(r.get("tags")),
            "notes": r.get("notes"),
            "customFields": [
                {
                    "key": cf.get("key"),
                    "label": cf.get("label"),
                    "type": cf.get("type"),
                    "value": cf.get("value"),
                }
                for cf in (r.get("customFields") or [])
                if isinstance(cf, dict)
            ],
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # Write set = the v3-writable customer fields: identity/contact/address, the
    # main project + origin channel ({id} refs on the v3 body), the free-field
    # values, the CRM note and tags. defaults + finance stay blue wishes until
    # upstream write coverage is field-verified (ADR-014; docs/05 #14 records
    # finance as UI-only today).
    _WRITABLE = {
        "name",
        "number",
        "email",
        "phone",
        "website",
        "vatId",
        "language",
        "notes",
        "customFields",
        "primaryAddress",
        "project",
        "channel",
        "tags",
    }
    # ONLY envelope keys a read emits and a write can never mean. Everything else
    # belongs in `rejected`: ADR-014 answers a write naming a non-writable field
    # with 409 + the field list, precisely so a migration cannot lose data without
    # noticing. `number` and `type` used to sit here — a merchant's own customer
    # number was accepted with 201 and silently replaced by a drawn one, which on a
    # 6k-record import destroys every foreign key with no error to see.
    _IGNORE = {
        "object",
        "id",
        "createdAt",
        "updatedAt",
    }

    @staticmethod
    def _ref_to_v3(value: Any) -> dict[str, str] | None:
        """A model reference ({id: "prj_7"} or a bare id) → the v3 ``{id}`` write
        shape with the speaking prefix stripped (ADR-002). ``None``/empty clears
        the assignment (the caller only emits the key when it is present)."""
        ident = value.get("id") if isinstance(value, dict) else value
        if ident in (None, ""):
            return None
        ident = str(ident)
        return {"id": ident.split("_", 1)[1] if "_" in ident else ident}

    @staticmethod
    def _addr_to_v3(a: dict[str, Any] | None) -> dict[str, Any]:
        a = a or {}
        return {
            k: a.get(src)
            for k, src in (
                ("name", "name"),
                ("contactPerson", "contactPerson"),
                ("street", "street"),
                ("zipCode", "zip"),
                ("city", "city"),
                ("state", "state"),
                ("country", "country"),
                ("gln", "gln"),
                # Also settable as flat `email`/`phone` on the record; the
                # address row wins, because it is the more specific statement.
                ("email", "email"),
                ("phone", "phone"),
            )
            if a.get(src) is not None
        }

    async def _send(  # noqa: ANN001
        self, base_url, token, method, up_handle, payload, accept_language, client
    ):
        """DELETE goes to v1 — v3 exposes no customer delete. Everything else keeps
        the v3 path (see _V1_PATH)."""
        if method.upper() != "DELETE":
            return await super()._send(
                base_url, token, method, up_handle, payload, accept_language, client
            )
        url = f"{base_url.rstrip('/')}{self._V1_PATH}/{up_handle}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.request("DELETE", url, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        v3: dict[str, Any] = {}
        rejected: set[str] = set()
        pa: dict[str, Any] = {}
        for key, src in (
            ("name", "name"),
            ("email", "email"),
            ("phone", "phone"),
        ):
            if src in model and model[src] is not None:
                pa[key] = model[src]
        if "primaryAddress" in model:
            pa.update(self._addr_to_v3(model["primaryAddress"]))
        if pa:
            v3["primaryAddress"] = pa
        # The merchant's own number, on CREATE only — upstream stores it verbatim and
        # falls back to the number range when absent. On UPDATE it is not writable, so
        # it lands in `rejected` (409) instead of being dropped.
        if "number" in model:
            if creating:
                if model["number"] is not None:
                    v3["number"] = model["number"]
            else:
                rejected.add("number")
        if "notes" in model:
            v3["notes"] = model["notes"]
        if "customFields" in model:
            cfs = custom_fields_to_v3(model["customFields"])
            if cfs is None:
                rejected.add("customFields")
            elif cfs:
                v3["customFields"] = cfs
        # vatId lives at v3 `financials.tax.vatId`, NOT on the address — writing it
        # onto primaryAddress (or a top-level `tax`) silently dropped it upstream.
        # v3 deep-merges financials.tax, so sending only vatId preserves taxNumber
        # / taxation / taxDisplay.
        if model.get("vatId") is not None:
            v3["financials"] = {"tax": {"vatId": model["vatId"]}}
        comm: dict[str, Any] = {}
        if "website" in model:
            comm["website"] = model["website"]
        if "language" in model:
            comm["language"] = model["language"]
        if comm:
            v3["communication"] = comm
        if "tags" in model:
            v3["tags"] = tags_to_v3(model["tags"])
        if "project" in model:
            v3["mainProject"] = self._ref_to_v3(model["project"])
        if "channel" in model:
            v3["originSalesChannel"] = self._ref_to_v3(model["channel"])
        for k in model:
            if k in self._WRITABLE or k in self._IGNORE:
                continue
            rejected.add(k)
        return v3, rejected
