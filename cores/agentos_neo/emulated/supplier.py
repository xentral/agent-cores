"""Xentral V3 facade · supplier — Stammdaten (docs/01-model.md §6.2).

Reads Xentral v3 ``/api/v3/suppliers`` (structurally like customer; contact fields
on ``primaryAddress``), plus a ``purchasing`` block and a creditor account. Per
ADR-014 only upstream-writable fields are creatable/updatable; the rest are blue
wishes.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .partner_subresources import (
    PartnerSubresourcesMixin,
    addresses_prop,
    bill_addr_from_dba,
    contacts_prop,
)
from .base import RO, FacadeAdapterBase, map_tags, prop, ref, tags_prop, tags_to_v3

_CU = {"creatable": True, "updatable": True}


class SupplierAdapter(PartnerSubresourcesMixin, FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Supplier",
        label_en="Supplier",
        category="masterdata",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.supplier",
        source_apis=("agentos_neo",),
        operations=("list", "read", "create", "update"),
    )
    v3_path = "/api/v3/suppliers"
    include = "tags"
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
        "purchasing": {"label": "Purchasing"},
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
                        "archive", "Archive", wish="No archive flag is writable via v1 suppliers."
                    ),
                    self.step_cmd(
                        "reactivate",
                        "Reactivate",
                        wish="No archive flag is writable via v1 suppliers.",
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "setHold", "Set hold", wish="Supplier holds are not writable via the public API."
            ),
            self.action_def(
                "releaseHold",
                "Release hold",
                wish="Supplier holds are not writable via the public API.",
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
            "type": prop("select", "Type", section="general"),
            "status": prop(
                "select",
                "Status",
                **RO,
                section="general",
                options=[{"value": v, "label": v.capitalize()} for v in ("active", "archived")],
            ),
            "parent": prop(
                "reference", "Parent (HQ)", **RO, section="general", reference="Supplier"
            ),
            "billTo": prop(
                "reference",
                "Bill-to (central billing)",
                **RO,
                section="general",
                reference="Supplier",
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
            # vatId is not a query field on v3 /suppliers (no filter/sort/search) —
            # writable only.
            "vatId": prop("string", "VAT id", section="general", **_CU),
            "language": prop("string", "Language", section="general", **_CU),
            # No separate primaryAddress block — the main address is the default row
            # (type "both", isDefault) of the unified addresses list.
            "purchasing": prop(
                "embedded",
                "Purchasing",
                section="purchasing",
                properties={
                    # deviatingSupplierNumber upstream: writable, but not a query
                    # field on v3 /suppliers (so no filter/sort/search).
                    "ourCustomerNumber": prop("string", "Our customer number", **_CU),
                    "confirmationRequired": prop("boolean", "Confirmation required"),
                    "sendOrdersVia": prop("select", "Send orders via"),
                    "deliveryDays": prop("integer", "Delivery days"),
                    "minimumOrderValue": prop(
                        "embedded",
                        "Minimum order value",
                        properties={
                            "amount": prop("string", "Amount"),
                            "currency": prop("string", "Currency"),
                        },
                    ),
                },
            ),
            "addresses": addresses_prop(prop, RO, _CU),
            "contacts": contacts_prop(prop, RO, _CU),
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
                    "shippingMethod": prop(
                        "reference", "Shipping method", **RO, reference="ShippingMethod"
                    ),
                    "priceList": prop("reference", "Price list", **RO, reference="PriceList"),
                    "partialShipping": prop("select", "Partial shipping", **RO),
                    "paymentTerms": prop(
                        "embedded",
                        "Payment terms",
                        properties={
                            "dueDays": prop("integer", "Due days"),
                            "discountPercent": prop("decimal", "Discount %"),
                            "discountDays": prop("integer", "Discount days"),
                        },
                    ),
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
                    "creditorAccountNumber": prop("string", "Creditor account"),
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
            "tags": tags_prop(writable=True),
            "customFields": prop("embedded", "Custom fields", section="general", properties={}),
            "createdAt": prop("datetime", "Created at", **RO, filterable=True, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, filterable=True, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        pa = r.get("primaryAddress") or {}
        comm = r.get("communication") or {}
        fin = r.get("financials") or {}
        mp = r.get("mainProject")
        return {
            "object": "supplier",
            "id": (f"sup_{r.get('id')}" if r.get("id") is not None else None),
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
            "purchasing": {
                "ourCustomerNumber": r.get("deviatingSupplierNumber"),
                "confirmationRequired": None,
                "sendOrdersVia": None,
                "deliveryDays": None,
                "minimumOrderValue": None,
            },
            # ONE unified address list: the main address (v3 primaryAddress) is the
            # default row (type "both"); the billing singleton rides in the v3 payload
            # (surfaced on EVERY row); shipping rows are appended on composed reads.
            "addresses": [
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
            "contacts": None,
            "defaults": {
                "shippingMethod": None,
                "priceList": None,
                "partialShipping": None,
                "currency": fin.get("currency"),
                "language": comm.get("language"),
                "paymentMethod": ref(
                    "paym_",
                    (fin.get("paymentMethod") or {}).get("id"),
                    None,
                    None,
                    "paymentMethods",
                ),
                "paymentTerms": {"dueDays": None, "discountPercent": None, "discountDays": None},
            },
            "finance": {"openAmount": None, "creditorAccountNumber": None},
            "project": ref(
                "prj_", mp.get("id") if isinstance(mp, dict) else mp, None, None, "projects"
            ),
            "tags": map_tags(r.get("tags")),
            "customFields": {},
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # Write set = the v3-writable supplier fields: identity/contact/address, the
    # main project ({id} ref), the deviating supplier number (via purchasing), and
    # tags. defaults + finance + the other purchasing fields stay blue wishes until
    # upstream write coverage is field-verified (ADR-014).
    _WRITABLE = {
        "name",
        "email",
        "phone",
        "website",
        "vatId",
        "language",
        "primaryAddress",
        "purchasing",
        "project",
        "tags",
    }
    # The only writable leaf inside the ``purchasing`` embed (→ v3
    # ``deviatingSupplierNumber``); every other purchasing field is a blue wish and
    # is rejected explicitly rather than silently dropped.
    _WRITABLE_PURCHASING = {"ourCustomerNumber"}
    _IGNORE = {
        "status",
        "parent",
        "billTo",
        "channels",
        "object",
        "id",
        "number",
        "type",
        "finance",
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
                ("street", "street"),
                ("zipCode", "zip"),
                ("city", "city"),
                ("state", "state"),
                ("country", "country"),
            )
            if a.get(src) is not None
        }

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
        purchasing = model.get("purchasing")
        if isinstance(purchasing, dict):
            if "ourCustomerNumber" in purchasing:
                v3["deviatingSupplierNumber"] = purchasing["ourCustomerNumber"]
            # the remaining purchasing fields are blue wishes — reject, not drop
            rejected.update(
                f"purchasing.{sub}" for sub in purchasing if sub not in self._WRITABLE_PURCHASING
            )
        for k in model:
            if k in self._WRITABLE or k in self._IGNORE:
                continue
            rejected.add(k)
        return v3, rejected
