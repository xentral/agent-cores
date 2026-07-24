from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import httpx

from ._search import extract_search, fan_out_search
from entity_registry.core_sdk import AdapterResponse, EmulationManifest
from .contact_email_action import execute_send_email_action, send_email_action_metadata
from .tag_action import execute_tag_action, tag_action_metadata

_TIMEOUT_SECONDS = 60.0


def _property(
    type_: str,
    *labels: str,
    **extra: Any,
) -> dict[str, Any]:
    label = labels[-1] if labels else ""
    return {"type": type_, "label": label, **extra}


def _postal_address_properties() -> dict[str, Any]:
    return {
        "name": _property("string", "Name"),
        "department": _property("string", "Department"),
        "subDepartment": _property("string", "Sub-department"),
        "street": _property("string", "Street"),
        "additionalAddressInformation": _property("string", "Additional address"),
        "contactPerson": _property("string", "Contact person"),
        "postalCode": _property("string", "Postal code"),
        "city": _property("string", "City"),
        "state": _property("string", "State"),
        "country": _property("string", "Country"),
        "vatId": _property("string", "VAT ID"),
    }


def _contact_person_properties() -> dict[str, Any]:
    return {
        "id": _property("string", "ID", access="readOnly"),
        "uuid": _property("string", "UUID", access="readOnly"),
        "businessPartnerId": _property(
            "reference", "Business partner", reference="BusinessPartner", renderProperty="name"
        ),
        "type": _property("string", "Type"),
        "language": _property("string", "Language"),
        "address": _property("embedded", "Address", properties=_postal_address_properties()),
        "salutation": _property("string", "Salutation"),
        "title": _property("string", "Title"),
        "firstName": _property("string", "First name"),
        "lastName": _property("string", "Last name"),
        "firstname": _property("string", "First name"),
        "lastname": _property("string", "Last name"),
        "name": _property("string", "Name"),
        "department": _property("string", "Department"),
        "position": _property("string", "Position"),
        "email": _property("string", "Email"),
        "phone": _property("string", "Phone"),
        "mobile": _property("string", "Mobile"),
        "fax": _property("string", "Fax"),
        "notes": _property("string", "Notes"),
        "area": _property("string", "Area"),
        "birthday": _property("date", "Birthday"),
        "internalComment": _property("string", "Internal comment"),
        "taxation": _property("string", "Taxation"),
        "marketingLocked": _property("string", "Marketing locked"),
        "isDeleted": _property("boolean", "Deleted"),
        "isPrimary": _property("boolean", "Primary"),
        "isBillingContact": _property("boolean", "Billing contact"),
        "isShippingContact": _property("boolean", "Shipping contact"),
        "createdAt": _property("datetime", "Created at", access="readOnly"),
        "updatedAt": _property("datetime", "Updated at", access="readOnly"),
    }


def _delivery_address_properties() -> dict[str, Any]:
    return {
        "id": _property("string", "ID", access="readOnly"),
        "uuid": _property("string", "UUID", access="readOnly"),
        "businessPartnerId": _property(
            "reference", "Business partner", reference="BusinessPartner", renderProperty="name"
        ),
        "type": _property("string", "Type"),
        "language": _property("string", "Language"),
        "address": _property("embedded", "Address", properties=_postal_address_properties()),
        "name": _property("string", "Name"),
        "department": _property("string", "Department"),
        "subDepartment": _property("string", "Sub-department"),
        "street": _property("string", "Street"),
        "additionalAddressInformation": _property("string", "Additional address"),
        "postalCode": _property("string", "Postal code"),
        "city": _property("string", "City"),
        "state": _property("string", "State"),
        "country": _property("string", "Country"),
        "contactPerson": _property("string", "Contact person"),
        "email": _property("string", "Email"),
        "phone": _property("string", "Phone"),
        "fax": _property("string", "Fax"),
        "mobile": _property("string", "Mobile"),
        "vatId": _property("string", "VAT ID"),
        "gln": _property("string", "GLN"),
        "title": _property("string", "Title"),
        "salutation": _property("string", "Salutation"),
        "notes": _property("string", "Notes"),
        "taxation": _property("string", "Taxation"),
        "vatExemption": _property("string", "VAT exemption"),
        "deliveryTerms": _property("string", "Delivery terms"),
        "internalComment": _property("string", "Internal comment"),
        "hint": _property("string", "Hint"),
        "isDefaultDeliveryAddress": _property("boolean", "Default delivery address"),
        "isDefault": _property("boolean", "Default"),
        "isBillingAddress": _property("boolean", "Billing address"),
        "isShippingAddress": _property("boolean", "Shipping address"),
        "createdAt": _property("datetime", "Created at", access="readOnly"),
        "updatedAt": _property("datetime", "Updated at", access="readOnly"),
    }


class SupplierAdapter:
    manifest = EmulationManifest(
        key="Supplier",
        label_en="Supplier",
        category="MasterData",
        rollout_batch="supplier-masterdata-v1",
        adapter="v3-supplier",
        source_apis=("/api/v3/suppliers",),
        operations=("list", "read", "create", "update", "delete"),
    )

    base_path = "/api/v3/suppliers"
    # NOTE: /api/v3/suppliers only allows these includes — verified against the
    # live endpoint: contactPersons, deliveryAddresses, tags, groups, customFields,
    # supplierRoles, financials.paymentMethod, communication.additionalContactInformation.
    # `primaryAddress`, `mainProject` and bare `communication` are REJECTED (HTTP 400)
    # and are unnecessary anyway — they come back as base fields without an include.
    list_include = "contactPersons,deliveryAddresses,tags"
    detail_include = "contactPersons,deliveryAddresses,tags,customFields"
    payload_read_only_fields = {
        "uuid",
        "id",
        "supplierNumber",
        "createdAt",
        "updatedAt",
        "contactPersons",
        "deliveryAddresses",
    }
    query_aliases = {
        "supplierNumber": "number",
        "name": "name",
        "email": "email",
        "phone": "phone",
        "mobile": "mobile",
        "city": "city",
        "zipCode": "zipCode",
        "state": "state",
        "country": "country",
        "mainProject": "mainProject.id",
        "tags": "tags",
    }
    client_side_filter_fields = {"supplierType", "status"}
    # Fields a clerk searches a supplier by. Emulated as an OR fan-out because
    # the v3 suppliers endpoint has no native cross-field `search` key.
    search_fields = ("supplierNumber", "name", "email")

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        language = "en"
        p = lambda type_, *labels, **extra: _property(type_, *labels, language=language, **extra)  # noqa: E731
        properties: dict[str, Any] = {
            "uuid": p("string", "UUID", access="readOnly"),
            "id": p("string", "ID", access="readOnly"),
            "supplierNumber": p(
                "string",
                "Supplier number",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            "supplierNumberAccounting": p(
                "string", "Supplier accounting number", section="references"
            ),
            "customerNumberAtSupplier": p(
                "string",
                "Customer number at supplier",
                previewable=True,
                section="references",
            ),
            "supplierNumberAtCustomer": p(
                "string", "Supplier number at customer", section="references"
            ),
            "identifier": p("string", "Identifier", section="references"),
            "ledgerAccount": p("string", "Ledger account", section="financials"),
            "type": p("string", "Type", section="general"),
            "supplierType": p(
                "string",
                "Supplier type",
                filterable=True,
                previewable=True,
                section="general",
            ),
            "status": p(
                "select",
                "Status",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
                options=[
                    {"value": "active", "label": "Active"},
                    {"value": "deleted", "label": "Deleted"},
                ],
            ),
            "partnerCategory": p("string", "Partner category", section="general"),
            "isDeleted": p("boolean", "Deleted", section="general"),
            "marketingLocked": p("string", "Marketing locked", section="general"),
            "trackingLocked": p("string", "Tracking locked", section="general"),
            "deliveryBlocked": p(
                "boolean", "Delivery blocked", previewable=True, section="general"
            ),
            "name": p(
                "string",
                "Name",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
                rules=["required"],
            ),
            "email": p(
                "string",
                "Email",
                filterable=True,
                searchable=True,
                previewable=True,
                section="communication",
            ),
            "phone": p("string", "Phone", previewable=True, section="communication"),
            "mobile": p("string", "Mobile", section="communication"),
            "fax": p("string", "Fax", section="communication"),
            "website": p("string", "Website", section="communication"),
            "language": p("string", "Language", section="communication"),
            "currency": p("string", "Currency", previewable=True, section="financials"),
            "vatId": p("string", "VAT ID", section="general"),
            "taxId": p("string", "Tax ID", section="financials"),
            "taxNumber": p("string", "Tax number", section="financials"),
            "taxation": p("string", "Taxation", section="financials"),
            "vatExemption": p("string", "VAT exemption", section="financials"),
            "supplierVat": p("string", "Supplier VAT", section="financials"),
            "isTaxExempt": p("boolean", "Tax exempt", section="financials"),
            "primaryAddress": p(
                "embedded",
                "Primary address",
                section="address",
                properties=_postal_address_properties(),
            ),
            "hasDeviatingBillingAddress": p(
                "boolean", "Has deviating billing address", section="address"
            ),
            "deviatingBillingAddress": p(
                "embedded",
                "Deviating billing address",
                section="address",
                properties=_postal_address_properties(),
            ),
            "contactPersons": p(
                "collection",
                "Contact persons",
                section="contacts",
                node={"properties": _contact_person_properties()},
            ),
            "deliveryAddresses": p(
                "collection",
                "Delivery addresses",
                section="address",
                node={"properties": _delivery_address_properties()},
            ),
            "communication": p(
                "embedded",
                "Communication",
                section="communication",
                properties={
                    "language": p("string", "Language"),
                    "website": p("string", "Website"),
                    "email": p("string", "Email"),
                    "phone": p("string", "Phone"),
                    "mobile": p("string", "Mobile"),
                    "fax": p("string", "Fax"),
                },
            ),
            "mainProject": p(
                "reference",
                "Main project",
                reference="Project",
                renderProperty="name",
                section="references",
            ),
            "deviatingCreditorAccountNumber": p(
                "string",
                "Deviating creditor account number",
                section="references",
            ),
            "gln": p("string", "GLN", section="general"),
            "paymentMethod": p(
                "reference",
                "Payment method",
                reference="PaymentMethod",
                renderProperty="name",
                section="financials",
            ),
            "paymentTargetDays": p("integer", "Payment target days", section="financials"),
            "paymentTargetDiscountDays": p("integer", "Discount days", section="financials"),
            "paymentTargetDiscount": p("decimal", "Discount", section="financials"),
            "isPaymentTermsFixed": p("boolean", "Payment terms fixed", section="financials"),
            "bankName": p("string", "Bank name", section="financials"),
            "accountHolder": p("string", "Account holder", section="financials"),
            "iban": p("string", "IBAN", section="financials"),
            "bic": p("string", "BIC", section="financials"),
            "bankAccountNumber": p("string", "Bank account number", section="financials"),
            "bankCode": p("string", "Bank code", section="financials"),
            "sepaMandateReference": p("string", "SEPA mandate reference", section="financials"),
            "sepaMandateIssuedAt": p("date", "SEPA mandate issued at", section="financials"),
            # Tags as a first-class `tag` field (see CustomerAdapter) — list-view
            # pills + column filter, same shape as the document entities.
            "tags": p("tag", "Tags", "Tags", filterable=True, section="segmentation"),
            "customFields": p(
                "collection",
                "Custom Fields",
                "Custom fields",
                section="customFields",
                access="readOnly",
                node={
                    "properties": {
                        "key": p("string", "Key", access="readOnly"),
                        "label": p("string", "Label", access="readOnly"),
                        "value": p("string", "Value", access="readOnly"),
                    }
                },
            ),
            "createdAt": p("datetime", "Created at", access="readOnly"),
            "updatedAt": p("datetime", "Updated at", access="readOnly"),
        }
        return {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "searchFields": list(self.search_fields),
            "previewTemplateString": "{{supplierNumber}} · {{name}}",
            "sections": {
                "general": {"label": "General"},
                "address": {"label": "Address"},
                "contacts": {"label": "Contacts"},
                "communication": {"label": "Communication"},
                "financials": {"label": "Financials"},
                "references": {"label": "References"},
                "segmentation": {"label": "Segmentation"},
                "customFields": {"label": "Custom Fields"},
            },
            "rootNode": {"properties": properties},
            "actions": [
                send_email_action_metadata(self.manifest.key),
                tag_action_metadata(self.manifest.key, "addTag"),
                tag_action_metadata(self.manifest.key, "removeTag"),
            ],
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }

    @classmethod
    def _ref(cls, value: Any) -> dict[str, Any] | None:
        if value in (None, ""):
            return None
        if isinstance(value, dict):
            if value.get("id") is not None:
                value["id"] = str(value["id"])
            return value
        return {"id": str(value)}

    @classmethod
    def _postal_address_from_v3(
        cls, value: dict[str, Any] | None, vat_id: Any = None
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict) and vat_id in (None, ""):
            return None
        source = value if isinstance(value, dict) else {}
        return {
            "name": source.get("name"),
            "department": source.get("department"),
            "subDepartment": source.get("subDepartment"),
            "street": source.get("street"),
            "additionalAddressInformation": source.get("addressSupplement"),
            "contactPerson": source.get("contactPerson"),
            "postalCode": source.get("zipCode"),
            "city": source.get("city"),
            "state": source.get("state"),
            "country": source.get("country"),
            "vatId": vat_id,
        }

    @staticmethod
    def _postal_address_to_v3(value: Any) -> tuple[dict[str, Any] | None, Any]:
        if value is None:
            return None, None
        if not isinstance(value, dict):
            raise ValueError("primaryAddress must be an object or null")
        field_map = {
            "additionalAddressInformation": "addressSupplement",
            "postalCode": "zipCode",
        }
        address: dict[str, Any] = {}
        vat_id = value.get("vatId")
        for key, field_value in value.items():
            if key == "vatId":
                continue
            address[field_map.get(key, key)] = field_value
        return address, vat_id

    @classmethod
    def _child_record_transform(cls, item: dict[str, Any]) -> dict[str, Any]:
        child = deepcopy(item)
        if child.get("id") is not None:
            child["id"] = str(child["id"])
            child["uuid"] = child["id"]
        if isinstance(child.get("address"), dict):
            child["address"] = cls._postal_address_from_v3(child["address"], child.get("vatId"))
        if "firstName" not in child and "firstname" in child:
            child["firstName"] = child.get("firstname")
        if "lastName" not in child and "lastname" in child:
            child["lastName"] = child.get("lastname")
        if "isDefaultDeliveryAddress" not in child and "isDefault" in child:
            child["isDefaultDeliveryAddress"] = child.get("isDefault")
        if "address" not in child and any(
            key in child for key in ("name", "street", "zipCode", "postalCode", "city", "country")
        ):
            child["address"] = cls._postal_address_from_v3(
                {
                    "name": child.get("name"),
                    "department": child.get("department"),
                    "subDepartment": child.get("subDepartment"),
                    "street": child.get("street"),
                    "addressSupplement": child.get("additionalAddressInformation")
                    or child.get("addressSupplement"),
                    "zipCode": child.get("postalCode") or child.get("zipCode"),
                    "city": child.get("city"),
                    "state": child.get("state"),
                    "country": child.get("country"),
                    "contactPerson": child.get("contactPerson"),
                },
                child.get("vatId"),
            )
        return child

    @classmethod
    def _entity_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(record)
        record["id"] = str(record.get("id")) if record.get("id") is not None else None
        if record.get("id") is not None:
            record["uuid"] = record["id"]
        record["supplierNumber"] = record.pop("number", record.pop("supplierNumber", None))
        record["supplierNumberAccounting"] = record.pop("supplierNumberAccounting", None)
        record["customerNumberAtSupplier"] = record.pop("customerNumberAtSupplier", None)
        record["supplierNumberAtCustomer"] = record.pop("supplierNumberAtCustomer", None)
        record["identifier"] = record.pop("identifier", None)
        record["ledgerAccount"] = record.pop("ledgerAccount", None)
        for key in (
            "type",
            "supplierType",
            "status",
            "partnerCategory",
            "isDeleted",
            "marketingLocked",
            "trackingLocked",
            "deliveryBlocked",
            "vatId",
            "taxId",
            "taxNumber",
            "taxation",
            "vatExemption",
            "supplierVat",
            "isTaxExempt",
            "currency",
            "bankName",
            "accountHolder",
            "iban",
            "bic",
            "bankAccountNumber",
            "bankCode",
            "sepaMandateReference",
            "sepaMandateIssuedAt",
            "paymentTargetDays",
            "paymentTargetDiscountDays",
            "paymentTargetDiscount",
            "isPaymentTermsFixed",
        ):
            if key not in record:
                record[key] = None
        if record.get("supplierType") is None:
            record["supplierType"] = record.get("type")
        record["status"] = "deleted" if record.get("isDeleted") is True else "active"
        vat_id = record.pop("vatId", None)
        raw_primary = record.pop("primaryAddress", None)
        record["primaryAddress"] = cls._postal_address_from_v3(raw_primary, vat_id)
        record["vatId"] = vat_id
        record["hasDeviatingBillingAddress"] = record.pop("hasDeviatingBillingAddress", None)
        record["deviatingBillingAddress"] = cls._postal_address_from_v3(
            record.pop("deviatingBillingAddress", None)
        )
        if record["primaryAddress"] is None:
            record["primaryAddress"] = cls._postal_address_from_v3(
                {
                    "name": record.pop("name", None),
                    "street": record.pop("street", None),
                    "zipCode": record.pop("zipCode", None),
                    "city": record.pop("city", None),
                    "state": record.pop("state", None),
                    "country": record.pop("country", None),
                    "contactPerson": record.pop("contactPerson", None),
                },
                vat_id,
            )
        # Surface the flat display name from the primary address (v3 has no root
        # `name`), mirroring CustomerAdapter — otherwise the overview Name column
        # and search render empty and fall back to the raw uuid.
        record["name"] = record.get("name") or (record["primaryAddress"] or {}).get("name") or None
        communication = deepcopy(record.pop("communication", None) or {})
        record["communication"] = communication
        # v3 keeps email/phone/fax/mobile on the primary address and only
        # web/language under `communication` — fall back through both so the
        # flat contact fields the schema declares are populated.
        address_src = raw_primary if isinstance(raw_primary, dict) else {}
        for key in ("email", "phone", "mobile", "fax"):
            record[key] = record.pop(key, None) or address_src.get(key)
        for key in ("website", "language"):
            record[key] = record.pop(key, None) or communication.get(key)
        record["mainProject"] = cls._ref(record.pop("mainProject", record.pop("project", None)))
        record["deviatingCreditorAccountNumber"] = record.pop(
            "deviatingCreditorAccountNumber", None
        )
        record["gln"] = record.pop("gln", None)
        record["paymentMethod"] = cls._ref(record.pop("paymentMethod", None))
        if isinstance(record.get("contactPersons"), list):
            record["contactPersons"] = [
                cls._child_record_transform(item) if isinstance(item, dict) else item
                for item in record["contactPersons"]
            ]
        if isinstance(record.get("deliveryAddresses"), list):
            record["deliveryAddresses"] = [
                cls._child_record_transform(item) if isinstance(item, dict) else item
                for item in record["deliveryAddresses"]
            ]
        if isinstance(record.get("tags"), list):
            record["tags"] = [
                {"id": str(item["id"]), "name": item.get("name")}
                if isinstance(item, dict) and item.get("id") is not None
                else item
                for item in record["tags"]
            ]
        if isinstance(record.get("customFields"), list):
            record["customFields"] = [
                {k: v for k, v in item.items() if k in {"key", "label", "value"}}
                if isinstance(item, dict)
                else item
                for item in record["customFields"]
            ]
        allowed = set(cls().metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in record.items() if key in allowed}

    @classmethod
    def _v3_payload(cls, body: bytes | None) -> dict[str, Any]:
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Supplier payload must be a JSON object")

        out: dict[str, Any] = {}
        nested_primary = payload.get("primaryAddress")
        if nested_primary is not None:
            primary_address, vat_id = cls._postal_address_to_v3(nested_primary)
            if primary_address:
                for key, value in primary_address.items():
                    if key == "additionalAddressInformation":
                        out["addressSupplement"] = value
                    elif key == "postalCode":
                        out["zipCode"] = value
                    else:
                        out[key] = value
            if vat_id is not None:
                out["vatId"] = vat_id
        nested_billing = payload.get("deviatingBillingAddress")
        if nested_billing is not None:
            billing_address, _ = cls._postal_address_to_v3(nested_billing)
            if billing_address is not None:
                out["deviatingBillingAddress"] = billing_address

        field_map = {
            "supplierNumber": "number",
            "gln": "gln",
            "deviatingCreditorAccountNumber": "deviatingCreditorAccountNumber",
            "supplierNumberAccounting": "supplierNumberAccounting",
            "customerNumberAtSupplier": "customerNumberAtSupplier",
            "supplierNumberAtCustomer": "supplierNumberAtCustomer",
            "identifier": "identifier",
            "ledgerAccount": "ledgerAccount",
            "type": "type",
            "supplierType": "type",
            "partnerCategory": "partnerCategory",
            "marketingLocked": "marketingLocked",
            "trackingLocked": "trackingLocked",
            "deliveryBlocked": "deliveryBlocked",
            "email": "email",
            "phone": "phone",
            "mobile": "mobile",
            "fax": "fax",
            "website": "website",
            "language": "language",
            "currency": "currency",
            "vatId": "vatId",
            "taxId": "taxId",
            "taxNumber": "taxNumber",
            "taxation": "taxation",
            "vatExemption": "vatExemption",
            "supplierVat": "supplierVat",
            "isTaxExempt": "isTaxExempt",
            "hasDeviatingBillingAddress": "hasDeviatingBillingAddress",
            "bankName": "bankName",
            "accountHolder": "accountHolder",
            "iban": "iban",
            "bic": "bic",
            "bankAccountNumber": "bankAccountNumber",
            "bankCode": "bankCode",
            "sepaMandateReference": "sepaMandateReference",
            "sepaMandateIssuedAt": "sepaMandateIssuedAt",
            "paymentTargetDays": "paymentTargetDays",
            "paymentTargetDiscountDays": "paymentTargetDiscountDays",
            "paymentTargetDiscount": "paymentTargetDiscount",
            "isPaymentTermsFixed": "isPaymentTermsFixed",
        }

        ref_fields = {"mainProject", "paymentMethod"}
        for key, value in payload.items():
            if key in {
                "id",
                "uuid",
                "createdAt",
                "updatedAt",
                "contactPersons",
                "deliveryAddresses",
            }:
                continue
            if key in {"primaryAddress", "deviatingBillingAddress"}:
                continue
            if key == "communication" and isinstance(value, dict):
                out["communication"] = deepcopy(value)
                continue
            target = field_map.get(key, key)
            if target in ref_fields and value not in (None, "") and not isinstance(value, dict):
                value = {"id": str(value)}
            out[target] = value
        return out

    @classmethod
    def _query(
        cls, query: list[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
        translated: list[tuple[str, str]] = []
        filters_by_prefix: dict[str, dict[str, str]] = {}
        for key, value in query:
            lookup = key[:-6] if key.endswith("[key]") else key
            if key.startswith("filter[") and "][" in key:
                prefix, suffix = key.rsplit("[", 1)
                filters_by_prefix.setdefault(prefix, {})[suffix.rstrip("]")] = value

        client_side_prefixes = {
            prefix
            for prefix, parts in filters_by_prefix.items()
            if parts.get("key") in cls.client_side_filter_fields
        }
        client_side_filters = [filters_by_prefix[prefix] for prefix in sorted(client_side_prefixes)]

        for key, value in query:
            if key.startswith("filter[") and "][" in key:
                prefix = key.rsplit("[", 1)[0]
                if prefix in client_side_prefixes:
                    continue
            if key.endswith("[key]"):
                value = cls.query_aliases.get(value, value)
            elif key == "sort":
                prefix = "-" if value.startswith("-") else ""
                sort_key = value[1:] if prefix else value
                value = prefix + cls.query_aliases.get(sort_key, sort_key)
            elif lookup in cls.query_aliases:
                value = cls.query_aliases[lookup] if value == "" else value
            translated.append((key, value))
        return translated, client_side_filters

    @staticmethod
    def _matches_client_side_filter(record: dict[str, Any], filter_: dict[str, str]) -> bool:
        key = filter_.get("key")
        op = filter_.get("op") or "equals"
        expected = filter_.get("value")
        actual = record.get(key)
        if actual is None:
            actual = ""
        actual_s = str(actual).casefold()
        expected_s = str(expected or "").casefold()
        if op == "equals":
            return actual_s == expected_s
        if op == "notEquals":
            return actual_s != expected_s
        if op == "in":
            return actual_s in {part.strip().casefold() for part in str(expected or "").split(",")}
        if op == "notIn":
            return actual_s not in {
                part.strip().casefold() for part in str(expected or "").split(",")
            }
        return True

    @classmethod
    def _apply_client_side_filters(
        cls,
        data: dict[str, Any],
        filters: list[dict[str, str]],
    ) -> None:
        if not filters or not isinstance(data.get("data"), list):
            return
        rows = [
            item
            for item in data["data"]
            if isinstance(item, dict)
            and all(cls._matches_client_side_filter(item, filter_) for filter_ in filters)
        ]
        data["data"] = rows
        extra = data.get("extra")
        if isinstance(extra, dict):
            extra["total"] = len(rows)
            extra["totalCount"] = len(rows)

    @staticmethod
    def _request_headers(token: str, accept_language: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "xentral-ai-agent",
            "X-Pagination": "table",
        }
        if accept_language:
            headers["Accept-Language"] = accept_language
        return headers

    @staticmethod
    def _response_supplier_id(response: httpx.Response) -> str | None:
        try:
            data = response.json()
            if isinstance(data, dict):
                candidate = data.get("data", data)
                if isinstance(candidate, dict) and candidate.get("id") is not None:
                    return str(candidate["id"])
        except Exception:
            return None
        location = response.headers.get("location") or response.headers.get("Location")
        if location:
            tail = location.rstrip("/").split("/")[-1]
            if tail:
                return tail
        return None

    @classmethod
    async def _sync_nested_collection(
        cls,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        supplier_id: str,
        collection: str,
        desired: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> httpx.Response | None:
        url = f"{base_url.rstrip('/')}/api/v3/suppliers/{supplier_id}/{collection}"
        current_response = await client.get(url, headers=headers)
        if current_response.status_code >= 400:
            return current_response
        try:
            current_supplier = current_response.json().get("data", {})
        except (AttributeError, ValueError):
            return current_response

        if isinstance(current_supplier, dict):
            existing_items = current_supplier.get(collection, [])
        elif isinstance(current_supplier, list):
            existing_items = current_supplier
        else:
            existing_items = []
        existing_ids = {
            str(item["id"])
            for item in existing_items
            if isinstance(item, dict) and item.get("id") is not None
        }
        desired_ids = {
            str(item["id"])
            for item in desired
            if isinstance(item, dict) and item.get("id") is not None
        }

        unknown_ids = desired_ids - existing_ids
        if unknown_ids:
            missing = ", ".join(sorted(unknown_ids))
            request = httpx.Request("PATCH", url)
            return httpx.Response(
                400,
                request=request,
                json={
                    "title": f"Invalid Supplier {collection[:-1]}",
                    "detail": f"Unknown id(s): {missing}",
                },
            )

        for item_id in existing_ids - desired_ids:
            response = await client.delete(f"{url}/{item_id}", headers=headers)
            if response.status_code >= 400:
                return response

        for item in desired:
            item_id = item.get("id")
            payload = deepcopy(item)
            payload.pop("uuid", None)
            if item_id is None:
                response = await client.post(url, json=payload, headers=headers)
            else:
                response = await client.patch(f"{url}/{item_id}", json=payload, headers=headers)
            if response.status_code >= 400:
                return response
        return None

    @classmethod
    def _entity_record_from_response(cls, record: dict[str, Any]) -> dict[str, Any]:
        return cls._entity_record(record)

    async def request(
        self,
        *,
        method: str,
        handle: str | None,
        query: list[tuple[str, str]],
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        method = method.upper()
        if method == "GET" and not handle:
            search = extract_search(query)
            if search is not None and search[0]:
                return await fan_out_search(
                    self,
                    query=query,
                    value=search[0],
                    op=search[1],
                    search_fields=self.search_fields,
                    base_url=base_url,
                    token=token,
                    accept_language=accept_language,
                    client=client,
                )
        path = self.base_path
        if handle:
            if not handle.isdigit():
                return self._json_response(
                    400,
                    {"title": "Invalid Supplier handle", "detail": "Expected the numeric V3 id."},
                )
            path = f"{path}/{handle}"

        params, client_side_filters = self._query(query)
        if method == "GET" and not any(key == "include" for key, _ in params):
            params.append(("include", self.detail_include if handle else self.list_include))

        request_body: dict[str, Any] | None = None
        desired_contact_persons: list[dict[str, Any]] = []
        desired_delivery_addresses: list[dict[str, Any]] = []
        if method in {"POST", "PATCH", "PUT"}:
            try:
                request_body = self._v3_payload(body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return self._json_response(
                    400,
                    {"title": "Invalid Supplier payload", "detail": str(exc)},
                )
            desired_contact_persons = deepcopy(request_body.pop("contactPersons", []) or [])
            desired_delivery_addresses = deepcopy(request_body.pop("deliveryAddresses", []) or [])

        headers = self._request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            url = f"{base_url.rstrip('/')}{path}"
            if method == "GET" and handle:
                detail = await request_client.get(url, params=params, headers=headers)
                if detail.status_code >= 400:
                    return detail
                try:
                    supplier = detail.json().get("data", {})
                except (AttributeError, ValueError):
                    return detail
                contacts_req = request_client.get(f"{url}/contactPersons", headers=headers)
                addresses_req = request_client.get(f"{url}/deliveryAddresses", headers=headers)
                contacts_resp, addresses_resp = await asyncio.gather(
                    contacts_req, addresses_req, return_exceptions=True
                )
                if not isinstance(contacts_resp, Exception) and contacts_resp.status_code == 200:
                    supplier["contactPersons"] = contacts_resp.json().get("data", [])
                else:
                    supplier["contactPersons"] = []
                if not isinstance(addresses_resp, Exception) and addresses_resp.status_code == 200:
                    supplier["deliveryAddresses"] = addresses_resp.json().get("data", [])
                else:
                    supplier["deliveryAddresses"] = []
                payload = self._entity_record_from_response(supplier)
                return httpx.Response(200, json={"data": payload}, request=detail.request)

            if method in {"PATCH", "PUT"} and handle and request_body is not None:
                response = await request_client.request(
                    method, url, params=params, json=request_body, headers=headers
                )
                if response.status_code >= 400:
                    return response
                if desired_contact_persons:
                    sync_error = await self._sync_nested_collection(
                        request_client,
                        base_url=base_url,
                        supplier_id=handle,
                        collection="contactPersons",
                        desired=desired_contact_persons,
                        headers=headers,
                    )
                    if sync_error is not None:
                        return sync_error
                if desired_delivery_addresses:
                    sync_error = await self._sync_nested_collection(
                        request_client,
                        base_url=base_url,
                        supplier_id=handle,
                        collection="deliveryAddresses",
                        desired=desired_delivery_addresses,
                        headers=headers,
                    )
                    if sync_error is not None:
                        return sync_error
                return await request_client.get(
                    url, params={"include": self.detail_include}, headers=headers
                )

            response = await request_client.request(
                method, url, params=params, json=request_body, headers=headers
            )
            if (
                method == "POST"
                and response.status_code < 400
                and (desired_contact_persons or desired_delivery_addresses)
            ):
                supplier_id = self._response_supplier_id(response)
                if supplier_id:
                    if desired_contact_persons:
                        sync_error = await self._sync_nested_collection(
                            request_client,
                            base_url=base_url,
                            supplier_id=supplier_id,
                            collection="contactPersons",
                            desired=desired_contact_persons,
                            headers=headers,
                        )
                        if sync_error is not None:
                            return sync_error
                    if desired_delivery_addresses:
                        sync_error = await self._sync_nested_collection(
                            request_client,
                            base_url=base_url,
                            supplier_id=supplier_id,
                            collection="deliveryAddresses",
                            desired=desired_delivery_addresses,
                            headers=headers,
                        )
                        if sync_error is not None:
                            return sync_error
                    return await request_client.get(
                        f"{base_url.rstrip('/')}{self.base_path}/{supplier_id}",
                        params={"include": self.detail_include},
                        headers=headers,
                    )
            return response

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400 or not response.content:
            return AdapterResponse(response.status_code, response.content, dict(response.headers))

        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(response.status_code, response.content, dict(response.headers))

        if isinstance(data, dict) and "data" in data:
            data["data"] = (
                self._entity_record_from_response(data["data"])
                if isinstance(data["data"], dict)
                else [
                    self._entity_record_from_response(item) if isinstance(item, dict) else item
                    for item in data["data"]
                ]
            )
            self._apply_client_side_filters(data, client_side_filters)
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return AdapterResponse(response.status_code, content, {"content-type": "application/json"})

    @staticmethod
    def _json_response(status_code: int, payload: dict[str, Any]) -> AdapterResponse:
        return AdapterResponse(
            status_code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            {"content-type": "application/json"},
        )

    async def action(
        self,
        *,
        action_key: str,
        handle: str | None,
        body: bytes | None,
        base_url: str,
        token: str,
        accept_language: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AdapterResponse:
        del handle
        if action_key in {"addTag", "removeTag"}:
            return await execute_tag_action(
                action_key=action_key,
                body=body,
                base_url=base_url,
                token=token,
                accept_language=accept_language,
                client=client,
                entity_key=self.manifest.key,
                entity_label=self.manifest.label("en"),
                base_path=self.base_path,
                detail_include=self.detail_include,
                request_headers=self._request_headers,
                json_response=self._json_response,
            )
        return await execute_send_email_action(
            action_key=action_key,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
            entity_key=self.manifest.key,
            entity_label=self.manifest.label("en"),
            base_path=self.base_path,
            detail_include=self.detail_include,
            request_headers=self._request_headers,
        )
