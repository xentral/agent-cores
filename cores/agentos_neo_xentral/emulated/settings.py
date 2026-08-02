"""Xentral V3 facade · settings lookups — the instance's configuration catalogue.

The values an operator has configured in their Xentral (payment methods, tax
rates, warehouses, projects, users, webhooks, …) as first-class read-only
entities, so an agent can enumerate valid values BEFORE creating a record that
references them — the capability the standalone ``xentral_erp_settings`` MCP
tool provides today, folded into the core.

Every upstream endpoint is a public OpenAPI GET (xentral/api-spec-public); the
endpoints speak three query dialects, declared per adapter via ``query_mode``:

  paged    filter[n][key/op/value] + page[number]/page[size]  (v1_paging)
  filters  filter[…] only — the endpoint rejects page params  (employees)
  none     no query params at all — the endpoint rejects any  (productsCategories, …)

Writes are not orchestrated yet: every adapter declares only the operations its
upstream read side proves (list, plus read where a ``/{id}`` GET exists). The
upstream PATCH/PUT surfaces (shippingMethods, webhooks, productsCategories, …)
are follow-ups, not silent gaps — the operations gate answers 405.

``TaxRate`` and ``TextTemplate`` have bespoke GET handling: taxRates is keyed by
a country path segment (modelled as a required-ish ``country`` filter, default
DE), and text-templates returns one settings OBJECT that we unfold into one
record per document type (the differently-shaped ``misc`` block is skipped).
"""

from __future__ import annotations

from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

from .base import _TIMEOUT, RO, FacadeAdapterBase, prop, ref


def _lookup_manifest(
    key: str,
    label: str,
    *,
    read: bool = False,
    create: bool = False,
    update: bool = False,
    delete: bool = False,
) -> EmulationManifest:
    return EmulationManifest(
        key=key,
        label_en=label,
        category="settings",
        rollout_batch="agentos_neo_xentral",
        adapter=f"agentos_neo_xentral.{key[0].lower()}{key[1:]}",
        source_apis=("agentos_neo_xentral",),
        operations=tuple(
            ["list"]
            + (["read"] if read else [])
            + (["create"] if create else [])
            + (["update"] if update else [])
            + (["delete"] if delete else [])
        ),
    )


class SettingsLookupBase(FacadeAdapterBase):
    """Shared base for the read-only settings lookups (three query dialects)."""

    # "paged" → base v1_paging handling; "filters" → filter[…] only; "none" → bare GET.
    query_mode: str = "paged"
    preview_template = "{{name}}"
    sections = {"general": {"label": "General"}}
    # None of these upstreams accept a flat ``sort`` key — never append a tiebreak.
    sort_tiebreak = None

    async def _get(
        self,
        base_url: str,
        token: str,
        *,
        handle: str | None,
        query: list[tuple[str, str]],
        accept_language: str | None,
        client: httpx.AsyncClient | None,
    ) -> tuple[int, Any]:
        if self.query_mode == "none":
            query = []
        elif self.query_mode == "filters":
            query = [(k, v) for k, v in query if k.startswith("filter[")]
        return await super()._get(
            base_url,
            token,
            handle=handle,
            query=query,
            accept_language=accept_language,
            client=client,
        )


def _project_ref(raw: Any) -> dict[str, Any] | None:
    """Upstream ``project`` arrives as ``{"id": "1"}`` (or a bare id)."""
    pid = raw.get("id") if isinstance(raw, dict) else raw
    return ref("prj_", pid, None, raw.get("name") if isinstance(raw, dict) else None, "projects")


def _project_prop(**flags: Any) -> dict[str, Any]:
    return prop(
        "reference",
        "Project",
        reference="Project",
        renderProperty="name",
        section="general",
        **RO,
        **flags,
    )


class PaymentMethodAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("PaymentMethod", "Payment method")
    v3_path = "/api/v1/paymentMethods"
    v1_paging = True

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "type": prop("string", "Type", **RO, section="general", filterable=True),
            "project": _project_prop(filterable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "paymentMethod",
            "id": (f"paym_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
            "type": r.get("type"),
            "project": _project_ref(r.get("project")),
        }


class ShippingMethodAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("ShippingMethod", "Shipping method", read=True)
    v3_path = "/api/v1/shippingMethods"
    v1_paging = True

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "module": prop("string", "Module", **RO, section="general"),
            "type": prop("string", "Type", **RO, section="general"),
            "project": _project_prop(),
            "supportsDeliveries": prop("boolean", "Supports deliveries", **RO, section="general"),
            "supportsReturns": prop("boolean", "Supports returns", **RO, section="general"),
            "shippingEmailBehaviour": prop(
                "string", "Shipping email behaviour", **RO, section="general"
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "shippingMethod",
            "id": (f"ship_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
            "module": r.get("module"),
            "type": r.get("type"),
            "project": _project_ref(r.get("project")),
            "supportsDeliveries": r.get("supportDeliveries"),
            "supportsReturns": r.get("supportReturns"),
            "shippingEmailBehaviour": r.get("shippingEmailBehaviour"),
        }


class ReturnReasonAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("ReturnReason", "Return reason")
    v3_path = "/api/v1/returnReasons"
    query_mode = "none"  # upstream takes flat ?project=/&language= params only

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "description": prop("string", "Description", **RO, section="general"),
            "language": prop("string", "Language", **RO, section="general"),
            "project": _project_prop(),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "returnReason",
            "id": (f"rsn_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
            "description": r.get("description"),
            "language": r.get("language"),
            "project": _project_ref(r.get("project")),
        }


class DeliveryTermAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("DeliveryTerm", "Delivery term")
    v3_path = "/api/v1/deliveryTerms"
    v1_paging = True
    query_aliases = {"name": "designation"}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string", "Name", **RO, section="general", filterable=True, previewable=True
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "deliveryTerm",
            "id": (f"dt_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
        }


class PaymentTermsGroupAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("PaymentTermsGroup", "Payment terms group", read=True)
    v3_path = "/api/v1/paymentTermsGroups"
    v1_paging = True

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "key": prop("string", "Key", **RO, section="general", filterable=True),
            "name": prop(
                "string", "Name", **RO, section="general", filterable=True, previewable=True
            ),
            "isActive": prop("boolean", "Active", **RO, section="general", filterable=True),
            "comment": prop("string", "Comment", **RO, section="general"),
            "paymentTerms": prop(
                "embedded",
                "Payment terms",
                **RO,
                section="general",
                properties={
                    "basicDiscount": prop("decimal", "Basic discount (%)", **RO),
                    "paymentTargetDays": prop("integer", "Payment target (days)", **RO),
                    "earlyPaymentDiscount": prop("decimal", "Early payment discount (%)", **RO),
                    "postageFreeFrom": prop("decimal", "Postage free from", **RO),
                },
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        pt = r.get("paymentTerms") if isinstance(r.get("paymentTerms"), dict) else {}
        return {
            "object": "paymentTermsGroup",
            "id": (f"ptg_{r.get('id')}" if r.get("id") is not None else None),
            "key": r.get("key"),
            "name": r.get("name"),
            "isActive": r.get("isActive"),
            "comment": r.get("comment"),
            "paymentTerms": {
                "basicDiscount": pt.get("basicDiscount"),
                "paymentTargetDays": pt.get("paymentTargetDays"),
                "earlyPaymentDiscount": pt.get("earlyPaymentDiscount"),
                "postageFreeFrom": pt.get("postageFreeFrom"),
            },
        }


class WarehouseAdapter(SettingsLookupBase):
    """Reads AND writes v1 — a warehouse can be opened and closed by name.

    The write goes to the same generation as the read: ``POST /api/v1/warehouses``
    takes ``designation`` and the new warehouse is in the list immediately, so you
    can find again what you just opened.

    The entity API also offers ``warehouse`` with full CRUD, and it was the wrong
    surface: its record is name-only (id/uuid/name/timestamps), it addresses by
    ``uuid`` while refusing to filter on ``id``, and going there would have split
    reads and writes across two generations for no gain. Measured before choosing:
    v1 POST answers 201, DELETE answers 204, and DELETE answers 409 "Warehouse
    cannot be deleted" while storage locations still hang off it — a guard worth
    surfacing rather than working around.
    """

    # ``wh_`` is the prefix StorageLocationAdapter already emits in its
    # ``warehouse`` reference — this entity makes that reference resolvable.
    manifest = _lookup_manifest("Warehouse", "Warehouse", create=True, update=True, delete=True)
    v3_path = "/api/v1/warehouses"
    v1_paging = True
    query_aliases = {"name": "designation"}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string",
                "Name",
                section="general",
                creatable=True,
                updatable=True,
                filterable=True,
                previewable=True,
            ),
            "project": _project_prop(),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """v1 calls the name ``designation`` and validates it: a body without it
        answers 400 ``designation is required``."""
        wire: dict[str, Any] = {}
        rejected: set[str] = set()
        if "name" in model:
            wire["designation"] = model["name"]
        # `project` sits on the read record but the create endpoint does not take
        # it — say so instead of letting a caller assume the warehouse landed there.
        for path in ("project", "id", "object"):
            if path in model:
                rejected.add(path)
        return wire, rejected

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "warehouse",
            "id": (f"wh_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("designation"),
            "project": _project_ref(r.get("project")),
        }


class ProjectAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("Project", "Project")
    v3_path = "/api/v1/projects"
    v1_paging = True

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string", "Name", **RO, section="general", filterable=True, previewable=True
            ),
            "keyName": prop("string", "Key name", **RO, section="general", filterable=True),
            "description": prop("string", "Description", **RO, section="general"),
            "currency": prop("string", "Currency", **RO, section="general", filterable=True),
            "normalTaxRate": prop("decimal", "Normal tax rate (%)", **RO, section="general"),
            "reducedTaxRate": prop("decimal", "Reduced tax rate (%)", **RO, section="general"),
            "defaultCustomerPaymentMethod": prop(
                "reference",
                "Default customer payment method",
                reference="PaymentMethod",
                renderProperty="name",
                section="general",
                **RO,
            ),
            "defaultSupplierPaymentMethod": prop(
                "reference",
                "Default supplier payment method",
                reference="PaymentMethod",
                renderProperty="name",
                section="general",
                **RO,
            ),
            "defaultShippingMethod": prop(
                "reference",
                "Default shipping method",
                reference="ShippingMethod",
                renderProperty="name",
                section="general",
                **RO,
            ),
            "storageProcess": prop("string", "Storage process", **RO, section="general"),
            "pickingProcess": prop("string", "Picking process", **RO, section="general"),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        def _method_ref(raw: Any, prefix: str, collection: str) -> dict[str, Any] | None:
            mid = raw.get("id") if isinstance(raw, dict) else raw
            return ref(prefix, mid, None, None, collection)

        return {
            "object": "project",
            "id": (f"prj_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "keyName": r.get("keyName"),
            "description": r.get("description"),
            "currency": r.get("currency"),
            "normalTaxRate": r.get("normalTaxRate"),
            "reducedTaxRate": r.get("reducedTaxRate"),
            "defaultCustomerPaymentMethod": _method_ref(
                r.get("defaultCustomerPaymentMethod"), "paym_", "paymentMethods"
            ),
            "defaultSupplierPaymentMethod": _method_ref(
                r.get("defaultSupplierPaymentMethod"), "paym_", "paymentMethods"
            ),
            "defaultShippingMethod": _method_ref(
                r.get("defaultShippingMethod"), "ship_", "shippingMethods"
            ),
            "storageProcess": r.get("storageProcess"),
            "pickingProcess": r.get("pickingProcess"),
        }


class UserAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("User", "User")
    v3_path = "/api/v2/users"  # same filter[…]/page[…] dialect as v1
    v1_paging = True
    preview_template = "{{username}}"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "username": prop(
                "string", "Username", **RO, section="general", filterable=True, previewable=True
            ),
            "email": prop("string", "Email", **RO, section="general", filterable=True),
            "isActive": prop("boolean", "Active", **RO, section="general", filterable=True),
            "isAdmin": prop("boolean", "Admin", **RO, section="general", filterable=True),
            "locale": prop("string", "Locale", **RO, section="general"),
            "contactId": prop("string", "Contact ID", **RO, section="general"),
            "globalUserId": prop("string", "Global user ID", **RO, section="general"),
            "createdAt": prop("datetime", "Created at", **RO, section="general"),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "user",
            "id": (f"usr_{r.get('id')}" if r.get("id") is not None else None),
            "username": r.get("username"),
            "email": r.get("email"),
            "isActive": r.get("isActive"),
            "isAdmin": r.get("isAdmin"),
            "locale": r.get("locale"),
            "contactId": (str(r["contactId"]) if r.get("contactId") is not None else None),
            "globalUserId": r.get("globalUserId"),
            "createdAt": r.get("createdAt"),
        }


class EmployeeAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("Employee", "Employee")
    v3_path = "/api/v1/employees"
    query_mode = "filters"  # filter[…] supported, page params rejected

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string", "Name", **RO, section="general", filterable=True, previewable=True
            ),
            "number": prop("string", "Number", **RO, section="general", filterable=True),
            "email": prop("string", "Email", **RO, section="general"),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "employee",
            "id": (f"emp_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "number": r.get("number"),
            "email": r.get("email"),
        }


class ProductCategoryAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("ProductCategory", "Product category", read=True)
    v3_path = "/api/v1/productsCategories"
    query_mode = "none"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "parent": prop(
                "reference",
                "Parent category",
                reference="ProductCategory",
                renderProperty="name",
                section="general",
                **RO,
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        parent = r.get("parent")
        pid = parent.get("id") if isinstance(parent, dict) else parent
        return {
            "object": "productCategory",
            "id": (f"pcat_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "parent": ref(
                "pcat_",
                pid,
                None,
                parent.get("name") if isinstance(parent, dict) else None,
                "productsCategories",
            ),
        }


class MerchandiseGroupAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("MerchandiseGroup", "Merchandise group", read=True)
    v3_path = "/api/v1/productsMerchandiseGroups"
    query_mode = "none"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "project": _project_prop(),
            "lastUsedProductNumber": prop(
                "string", "Last used product number", **RO, section="general"
            ),
            "useMainProductNumberRange": prop(
                "boolean", "Uses main product number range", **RO, section="general"
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "merchandiseGroup",
            "id": (f"mg_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "project": _project_ref(r.get("project")),
            "lastUsedProductNumber": r.get("lastUsedProductNumber"),
            "useMainProductNumberRange": r.get("useMainProductNumberRange"),
        }


class ProductPropertyAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("ProductProperty", "Product property")
    v3_path = "/api/v1/productsProperties"
    query_mode = "none"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "project": _project_prop(),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "productProperty",
            "id": (f"pprop_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "project": _project_ref(r.get("project")),
        }


class ProductTagAdapter(SettingsLookupBase):
    # Distinct from ``Tag`` (the BF document-tag catalogue): productsTags is the
    # separate product-tag lookup (name + color) products are tagged with.
    manifest = _lookup_manifest("ProductTag", "Product tag")
    v3_path = "/api/v1/productsTags"
    v1_paging = True

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string", "Name", **RO, section="general", filterable=True, previewable=True
            ),
            "color": prop("string", "Color", **RO, section="general", previewable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "productTag",
            "id": (f"ptag_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "color": r.get("color"),
        }


class ProductFreeFieldAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("ProductFreeField", "Product free field", read=True)
    v3_path = "/api/v1/productsFreeFields"
    v1_paging = True

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string", "Name", **RO, section="general", filterable=True, previewable=True
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "productFreeField",
            "id": (f"pff_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
        }


class AddressCustomFieldAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("AddressCustomField", "Address custom field")
    v3_path = "/api/v2/settings/masterdata/addressCustomFields"
    query_mode = "none"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "type": prop(
                "select",
                "Type",
                **RO,
                section="general",
                options=[
                    {"value": v, "label": v.replace("_", " ").capitalize()}
                    for v in ("single_line", "multi_line", "date", "checkbox", "select")
                ],
            ),
            "allowedValues": prop(
                "collection",
                "Allowed values",
                **RO,
                section="general",
                node={
                    "properties": {
                        "label": prop("string", "Label", **RO),
                        "value": prop("string", "Value", **RO),
                    }
                },
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "addressCustomField",
            "id": (f"acf_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "type": r.get("type"),
            "allowedValues": [
                {"label": v.get("label"), "value": v.get("value")}
                for v in (r.get("allowedValues") or [])
                if isinstance(v, dict)
            ],
        }


class WebhookAdapter(SettingsLookupBase):
    manifest = _lookup_manifest("Webhook", "Webhook", read=True)
    v3_path = "/api/v1/webhooks"
    query_mode = "none"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "url": prop("string", "URL", **RO, section="general"),
            "events": prop(
                "collection",
                "Subscribed events",
                **RO,
                section="general",
                node={"properties": {"id": prop("string", "Event type", **RO)}},
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "webhook",
            "id": (f"wbh_{r.get('id')}" if r.get("id") is not None else None),
            "name": r.get("name"),
            "url": r.get("url"),
            "events": [
                {"id": e.get("id") if isinstance(e, dict) else e} for e in (r.get("events") or [])
            ],
        }


class WebhookEventTypeAdapter(SettingsLookupBase):
    # Upstream ids are event names ("com.xentral.salesOrder.created.v1"), not
    # numeric — exposed verbatim (no speaking prefix; list-only, so the id never
    # travels through the prefix-stripping read path).
    manifest = _lookup_manifest("WebhookEventType", "Webhook event type")
    v3_path = "/api/v1/webhookEventTypes"
    query_mode = "none"
    preview_template = "{{id}}"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "Event type", **RO, section="general", previewable=True),
            "group": prop("string", "Group", **RO, section="general"),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "webhookEventType",
            "id": r.get("id"),
            "group": r.get("group"),
        }


class TaxRateAdapter(SettingsLookupBase):
    """``GET /api/v1/taxRates/{countryCode}`` — the country is a PATH segment,
    modelled as a ``country`` filter (default DE). Rows carry no upstream id, so
    the synthesized id is positional (``tax_DE_1``) and list-only is declared."""

    manifest = _lookup_manifest("TaxRate", "Tax rate")
    v3_path = "/api/v1/taxRates"  # + /{countryCode}, built per request
    preview_template = "{{name}}"
    # Model filter key → upstream filter key (applied in the bespoke GET below).
    _filter_aliases = {"type": "type", "date": "date", "product": "productId"}

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "country": prop(
                "string",
                "Country (ISO-2)",
                **RO,
                section="general",
                filterable=True,
                description="Country the rate applies to; filter with equals, default DE.",
            ),
            "rate": prop("decimal", "Rate (%)", **RO, section="general", previewable=True),
            # Live values ("normal", "reduced", "") diverge from the OpenAPI
            # description ("standard, reduced or custom") — a plain string keeps
            # the contract honest.
            "type": prop("string", "Type", **RO, section="general", filterable=True),
            "name": prop("string", "Name", **RO, section="general", previewable=True),
            "source": prop("string", "Source", **RO, section="general"),
            "date": prop("date", "Valid on", **RO, section="general", filterable=True),
            "product": prop(
                "reference",
                "Product",
                reference="Product",
                renderProperty="name",
                section="general",
                filterable=True,
                **RO,
            ),
            "project": _project_prop(),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - request() maps
        return r

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
        if method.upper() != "GET" or handle:
            # Writes and reads fall through to the operations gate (405).
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
        # This override builds its own upstream call, so the base class's filter
        # guard never runs here — and the loop below DROPS any key it has no alias
        # for, which answers 200 with an unfiltered collection. Refuse first.
        refusal = self.refuse_undeclared_filters(query)
        if refusal is not None:
            return refusal
        country = "DE"
        params: list[tuple[str, str]] = []
        # First pass: resolve each filter index to its model key.
        key_by_index = {
            k[len("filter[") : k.index("]")]: v
            for k, v in query
            if k.startswith("filter[") and k.endswith("][key]")
        }
        # The upstream page param requires number AND size together (a lone
        # page[size] is a 400) — normalize to always send both, size 10..50.
        q = dict(query)
        try:
            page_number = max(1, int(q.get("page[number]") or "1"))
        except ValueError:
            page_number = 1
        try:
            page_size = max(10, min(50, int(q.get("page[size]") or "25")))
        except ValueError:
            page_size = 25
        params += [("page[number]", str(page_number)), ("page[size]", str(page_size))]
        out_idx = 0
        emitted: dict[str, int] = {}
        for k, v in query:
            if k.startswith("page["):
                continue
            if not k.startswith("filter["):
                continue
            idx = k[len("filter[") : k.index("]")]
            model_key = key_by_index.get(idx)
            if model_key == "country":
                if k.endswith("][value]") and str(v).strip():
                    country = str(v).strip().upper()
                continue
            upstream_key = self._filter_aliases.get(model_key or "")
            if not upstream_key:
                continue
            if idx not in emitted:
                emitted[idx] = out_idx
                out_idx += 1
            oi = emitted[idx]
            if k.endswith("][key]"):
                params.append((f"filter[{oi}][key]", upstream_key))
            elif k.endswith("][value]"):
                value = str(v)
                if model_key == "product" and "_" in value:
                    value = value.split("_", 1)[1]
                params.append((f"filter[{oi}][value]", value))
            else:
                params.append((f"filter[{oi}][op]", str(v)))
        url = f"{base_url.rstrip('/')}{self.v3_path}/{country}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.get(url, params=params, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400:
            return self._json(
                resp.status_code,
                payload if isinstance(payload, dict) else {"title": "upstream error"},
            )
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        mapped = []
        for i, r in enumerate(rows, start=1):
            if not isinstance(r, dict):
                continue
            product = r.get("product")
            project = r.get("project")
            mapped.append(
                {
                    "object": "taxRate",
                    "id": f"tax_{country}_{i}",
                    "country": country,
                    "rate": r.get("rate"),
                    "type": r.get("type"),
                    "name": r.get("name"),
                    "source": r.get("source"),
                    "date": r.get("date"),
                    "product": ref(
                        "prd_",
                        product.get("id") if isinstance(product, dict) else product,
                        None,
                        None,
                        "products",
                    ),
                    "project": _project_ref(project),
                }
            )
        extra = payload.get("extra") if isinstance(payload, dict) else None
        extra = dict(extra) if isinstance(extra, dict) else {}
        if "total" not in extra and isinstance(extra.get("totalCount"), int):
            extra["total"] = extra["totalCount"]
        return self._json(200, {"data": mapped, "extra": extra})


# The nine identically-shaped text-template blocks; the upstream ``misc`` block
# has a different shape (footer-order toggles) and is deliberately not exposed.
_TEXT_TEMPLATE_TYPES = (
    "offer",
    "order",
    "invoice",
    "delivery_note",
    "credit_note",
    "purchase_order",
    "work_report",
    "commission_credit_note",
    "proforma_invoice",
)


class TextTemplateAdapter(SettingsLookupBase):
    """``GET /api/v2/settings/text-templates`` returns ONE settings object keyed
    by document type — unfolded into one record per type (``tpl_offer``, …)."""

    manifest = _lookup_manifest("TextTemplate", "Text template", read=True)
    v3_path = "/api/v2/settings/text-templates"
    query_mode = "none"
    preview_template = "{{documentType}}"

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "documentType": prop(
                "select",
                "Document type",
                **RO,
                section="general",
                previewable=True,
                options=[{"value": v, "label": v.replace("_", " ")} for v in _TEXT_TEMPLATE_TYPES],
            ),
            "header": prop("string", "Header text (HTML)", **RO, section="general"),
            "footer": prop("string", "Footer text (HTML)", **RO, section="general"),
            "disableStationery": prop("boolean", "Stationery disabled", **RO, section="general"),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - request() maps
        return r

    @staticmethod
    def _record(doc_type: str, block: Any) -> dict[str, Any]:
        block = block if isinstance(block, dict) else {}
        return {
            "object": "textTemplate",
            "id": f"tpl_{doc_type}",
            "documentType": doc_type,
            "header": block.get("header"),
            "footer": block.get("footer"),
            "disableStationery": block.get("disable_stationary"),
        }

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
        if method.upper() != "GET":
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
        # The list path below returns every template type and applies no filter at
        # all, so an undeclared key would read as a filtered result. The base
        # class's guard cannot see this override — run it here.
        if not handle:
            refusal = self.refuse_undeclared_filters(query)
            if refusal is not None:
                return refusal
        url = f"{base_url.rstrip('/')}{self.v3_path}"
        headers = self._headers(token, accept_language)

        async def _do(c: httpx.AsyncClient) -> httpx.Response:
            return await c.get(url, headers=headers)

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await _do(c)
        else:
            resp = await _do(client)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400:
            return self._json(
                resp.status_code,
                payload if isinstance(payload, dict) else {"title": "upstream error"},
            )
        data = payload.get("data") if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        if handle:
            doc_type = handle.split("_", 1)[1] if handle.startswith("tpl_") else handle
            if doc_type not in _TEXT_TEMPLATE_TYPES:
                return self._json(404, {"title": f"TextTemplate {handle} not found"})
            return self._json(200, {"data": self._record(doc_type, data.get(doc_type))})
        records = [self._record(t, data.get(t)) for t in _TEXT_TEMPLATE_TYPES]
        return self._json(200, {"data": records, "extra": {"total": len(records)}})


SETTINGS_ADAPTERS = (
    PaymentMethodAdapter(),
    ShippingMethodAdapter(),
    ReturnReasonAdapter(),
    DeliveryTermAdapter(),
    PaymentTermsGroupAdapter(),
    TaxRateAdapter(),
    WarehouseAdapter(),
    ProjectAdapter(),
    UserAdapter(),
    EmployeeAdapter(),
    ProductCategoryAdapter(),
    MerchandiseGroupAdapter(),
    ProductPropertyAdapter(),
    ProductTagAdapter(),
    ProductFreeFieldAdapter(),
    AddressCustomFieldAdapter(),
    WebhookAdapter(),
    WebhookEventTypeAdapter(),
    TextTemplateAdapter(),
)
