from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx

from entity_registry.core_sdk import AdapterResponse, EmulationManifest

_TIMEOUT_SECONDS = 60.0

# Media type that selects the legacy v1-beta SalesOrder fulfillment API
# (``/api/salesOrders/{id}/actions/...``) — distinct from the v3 action family
# (``/api/v3/<entity>/{id}/actions/...``) which needs no special Accept header.
_V1_BETA_MEDIA_TYPE = "application/vnd.xentral.default.v1-beta+json"

# Human labels for the documentStatus process-step commands.
_LIFECYCLE_LABELS = {
    "release": "Release",
    "cancel": "Cancel",
    "complete": "Complete",
    "start": "Start",
    "dispatch": "Dispatch",
}

# Past-tense phrase per action for the normalized success message.
_ACTION_PHRASES = {
    "sendToRecipient": "sent to recipient",
    "release": "released",
    "cancel": "cancelled",
    "complete": "completed",
    "start": "started",
    "dispatch": "dispatched",
    "setWriteProtection": "write-protected",
    "removeWriteProtection": "write protection removed",
    "logActivity": "activity logged",
    "createPartialSalesOrder": "partial sales order created",
}


def allowed_action_keys(adapter: Any) -> set[str]:
    """The BF action keys a document adapter exposes, derived from its
    capability flags. Used by both metadata() and the action() dispatcher so the
    surfaced set and the executable set can never diverge."""
    keys: set[str] = set(adapter.lifecycle_actions)
    if adapter.supports_send:
        keys.add("sendToRecipient")
    if adapter.supports_write_protection:
        keys.update({"setWriteProtection", "removeWriteProtection"})
    if adapter.supports_log_activity:
        keys.add("logActivity")
    if adapter.supports_dispatch:
        keys.add("dispatch")
    if adapter.supports_create_partial_sales_order:
        keys.add("createPartialSalesOrder")
    if getattr(adapter, "supports_tags", False):
        keys.update({"addTag", "removeTag"})
    return keys


def build_process_steps(adapter: Any) -> list[dict[str, Any]]:
    """The documentStatus process-step group built from ``lifecycle_actions``
    (+ the SalesOrder-only ``dispatch`` transition). Returns ``[]`` for plain
    documents so the wire stays lean."""
    commands: list[dict[str, Any]] = []
    for key in adapter.lifecycle_actions:
        command: dict[str, Any] = {"key": key, "label": _LIFECYCLE_LABELS.get(key, key)}
        if key == "release" and adapter.release_has_document_date:
            command["command"] = {
                "type": "object",
                "properties": {
                    "documentDate": {
                        "type": "string",
                        "format": "date",
                        "label": "Document date",
                    },
                },
            }
        commands.append(command)
    if adapter.supports_dispatch:
        commands.append(
            {
                "key": "dispatch",
                "label": _LIFECYCLE_LABELS["dispatch"],
                "description": "Dispatch the sales order. Requires status 'released'.",
            }
        )
    groups: list[dict[str, Any]] = []
    if commands:
        groups.append({"key": "documentStatus", "label": "Document status", "commands": commands})
    # Write protection toggles the entity's ``isWriteProtected`` flag — it
    # changes the document's own internal state, so it is a process-step
    # command, not a (state-neutral) free action.
    if adapter.supports_write_protection:
        groups.append(
            {
                "key": "writeProtection",
                "label": "Write protection",
                "commands": [
                    {
                        "key": "setWriteProtection",
                        "label": "Set write protection",
                        "description": "Lock this document against edits; re-apply after a correction.",
                    },
                    {
                        "key": "removeWriteProtection",
                        "label": "Remove write protection",
                        "description": (
                            "Unlock a finalized document so its fields can be edited. The "
                            "customer may already hold it — only with the human's explicit "
                            "go-ahead; re-protect right after."
                        ),
                    },
                ],
            }
        )
    return groups


def build_document_actions(adapter: Any) -> list[dict[str, Any]]:
    """State-neutral entity actions beyond ``sendToRecipient`` — activity logging
    and the SalesOrder-only partial-order creator. Operations that change the
    document's own state (status transitions, write protection) are process
    steps, not actions (see ``build_process_steps``). Each maps to a real Xentral
    action endpoint resolved in ``execute_document_action``."""
    key = adapter.manifest.key
    actions: list[dict[str, Any]] = []
    if adapter.supports_log_activity:
        actions.append(
            {
                "key": "logActivity",
                "label": "Log activity",
                "bulk": False,
                "method": "PATCH",
                "path": f"/api/entity/{key}/actions/logActivity",
                "destructive": False,
                "description": "Record a custom activity log entry on this document.",
                "command": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string", "label": "Message"}},
                },
            }
        )
    if adapter.supports_create_partial_sales_order:
        actions.append(
            {
                "key": "createPartialSalesOrder",
                "label": "Create partial sales order",
                "bulk": False,
                "method": "PATCH",
                "path": f"/api/entity/{key}/actions/createPartialSalesOrder",
                "destructive": False,
                "description": (
                    "Create a partial sales order from the undispatched remainder. "
                    "Returns the newly created sales order."
                ),
            }
        )
    if getattr(adapter, "supports_tags", False):
        tag_command = {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string", "label": "Tag"}},
        }
        actions.append(
            {
                "key": "addTag",
                "label": "Add tag",
                "bulk": False,
                "method": "PATCH",
                "path": f"/api/entity/{key}/actions/addTag",
                "destructive": False,
                "description": "Add a tag to this document (created automatically if new).",
                "command": tag_command,
            }
        )
        actions.append(
            {
                "key": "removeTag",
                "label": "Remove tag",
                "bulk": False,
                "method": "PATCH",
                "path": f"/api/entity/{key}/actions/removeTag",
                "destructive": False,
                "description": "Remove a tag from this document.",
                "command": tag_command,
            }
        )
    return actions


def _action_upstream_payload(
    adapter: Any, action_key: str, command: dict[str, Any]
) -> dict[str, Any]:
    if action_key == "sendToRecipient":
        return adapter._action_payload(command)
    if action_key == "release":
        document_date = command.get("documentDate")
        if adapter.release_has_document_date and document_date not in (None, ""):
            return {"documentDate": document_date}
        return {}
    if action_key == "logActivity":
        return {"message": command.get("message", "")}
    return {}


def _read_tag_titles(record: dict[str, Any]) -> list[str]:
    """Extract the tag titles from a raw v3 document record's ``tags`` list."""
    tags = record.get("tags") if isinstance(record, dict) else None
    titles: list[str] = []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                value = tag.get("title") or tag.get("name")
                if value:
                    titles.append(str(value))
            elif isinstance(tag, str) and tag:
                titles.append(tag)
    return titles


async def _perform_tag_mutation(
    adapter: Any,
    *,
    action_key: str,
    document_id: str,
    command: dict[str, Any],
    base: str,
    headers: dict[str, str],
    client: httpx.AsyncClient | None,
) -> AdapterResponse:
    """addTag / removeTag via the v3 main endpoint. Since a document update
    *replaces* the whole ``tags`` list, this reads the current tags, adds or
    removes the given title, and PATCHes the full list back. New tags are
    created automatically by Xentral."""
    title = str(command.get("title") or "").strip()
    if not title:
        return adapter._json_response(422, {"message": f"{action_key} needs a tag title."})
    doc_url = f"{base}{adapter.base_path}/{document_id}"

    async def _run(rc: httpx.AsyncClient) -> AdapterResponse:
        current = await rc.get(doc_url, params={"include": "tags"}, headers=headers)
        if current.status_code >= 400:
            return AdapterResponse(
                current.status_code, current.content, adapter._forward_headers(current)
            )
        body = current.json() if current.content else {}
        record = body.get("data", body) if isinstance(body, dict) else {}
        titles = _read_tag_titles(record if isinstance(record, dict) else {})
        if action_key == "addTag":
            if title not in titles:
                titles.append(title)
        else:  # removeTag
            titles = [t for t in titles if t != title]
        patched = await rc.patch(
            doc_url, json={"tags": [{"title": t} for t in titles]}, headers=headers
        )
        if patched.status_code >= 400:
            return AdapterResponse(
                patched.status_code, patched.content, adapter._forward_headers(patched)
            )
        verb = "added to" if action_key == "addTag" else "removed from"
        return adapter._json_response(
            200,
            {
                "message": f"Tag '{title}' {verb} {adapter.manifest.label('en')} {document_id}.",
                "data": {"id": document_id, "tags": [{"title": t} for t in titles]},
            },
        )

    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as rc:
            return await _run(rc)
    return await _run(client)


async def execute_document_action(
    adapter: Any,
    *,
    action_key: str,
    body: bytes | None,
    base_url: str,
    token: str,
    accept_language: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> AdapterResponse:
    """Generic BF action dispatcher shared by every business-document adapter.

    Translates the ``{ids, command}`` envelope into the matching upstream
    Xentral call: the v3 action family (all PATCH, ``base_path``-relative) plus
    the two legacy v1-beta SalesOrder fulfillment actions (``dispatch`` /
    ``createPartialSalesOrder``)."""
    if action_key not in allowed_action_keys(adapter):
        return adapter._json_response(
            404, {"message": f"Unknown {adapter.manifest.key} action: {action_key}"}
        )
    try:
        envelope = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return adapter._json_response(422, {"message": f"Invalid action envelope: {exc}"})
    if not isinstance(envelope, dict):
        return adapter._json_response(422, {"message": "Action envelope must be a JSON object."})
    ids = envelope.get("ids")
    if not isinstance(ids, list) or not ids:
        return adapter._json_response(422, {"message": "ids must be a non-empty array."})
    if len(ids) != 1:
        return adapter._json_response(
            422, {"message": f"{action_key} only supports one document id."}
        )
    document_id = str(ids[0]).strip()
    if not document_id.isdigit():
        return adapter._json_response(422, {"message": "Document id must be numeric."})
    command = envelope.get("command") or {}
    if not isinstance(command, dict):
        return adapter._json_response(422, {"message": "command must be an object."})
    if action_key == "logActivity" and not str(command.get("message", "")).strip():
        return adapter._json_response(422, {"message": "logActivity requires a non-empty message."})

    headers = dict(adapter._request_headers(token, accept_language))
    base = base_url.rstrip("/")
    forward_raw = False
    payload: dict[str, Any] | None

    if action_key in {"addTag", "removeTag"}:
        # Tags are a normal v3 field; add/remove is a read-modify-write on the
        # main endpoint (update replaces the whole tag list).
        return await _perform_tag_mutation(
            adapter,
            action_key=action_key,
            document_id=document_id,
            command=command,
            base=base,
            headers=headers,
            client=client,
        )

    if action_key in {"dispatch", "createPartialSalesOrder"}:
        # Legacy v1-beta fulfillment API: absolute path, beta media type, no body.
        headers["Accept"] = _V1_BETA_MEDIA_TYPE
        headers["Content-Type"] = _V1_BETA_MEDIA_TYPE
        method = "POST" if action_key == "dispatch" else "PATCH"
        url = f"{base}/api/salesOrders/{document_id}/actions/{action_key}"
        payload = None
        # createPartialSalesOrder mints a new document — forward the upstream
        # response verbatim so its id / Location reach the caller unchanged.
        forward_raw = action_key == "createPartialSalesOrder"
    else:
        # v3 action family — all PATCH, relative to the entity base_path.
        v3_action = "send" if action_key == "sendToRecipient" else action_key
        method = "PATCH"
        url = f"{base}{adapter.base_path}/{document_id}/actions/{v3_action}"
        payload = _action_upstream_payload(adapter, action_key, command)

    async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
        return await request_client.request(method, url, json=payload, headers=headers)

    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
            response = await _perform(request_client)
    else:
        response = await _perform(client)

    if response.status_code >= 400 or forward_raw:
        return AdapterResponse(
            response.status_code, response.content, adapter._forward_headers(response)
        )
    if response.content:
        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code, response.content, adapter._forward_headers(response)
            )
    else:
        data = {"data": {"id": document_id}}
    phrase = _ACTION_PHRASES.get(action_key, "updated")
    return adapter._json_response(
        200,
        {
            "message": f"{adapter.manifest.label('en')} {document_id} {phrase}.",
            "data": data.get("data", data) if isinstance(data, dict) else data,
        },
        response.headers,
    )


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
        "country": _property("string", "Country"),
        "vatId": _property("string", "VAT ID"),
    }


def _base_line_item_properties() -> dict[str, Any]:
    return {
        "type": _property("string", "Type", access="readOnly"),
        "id": _property("string", "ID", access="readOnly"),
        "uuid": _property("string", "UUID", access="readOnly"),
        "sort": _property("integer", "Sort order"),
        "number": _property("string", "Product number", access="readOnly"),
        "name": _property("string", "Name"),
        "description": _property("string", "Description"),
        "quantity": _property("decimal", "Quantity"),
        "deliveredQuantity": _property("decimal", "Delivered quantity"),
        "unit": _property("string", "Unit"),
        "deliveryDate": _property("date", "Delivery date"),
        "deliveryDateAsCalendarWeek": _property("boolean", "Delivery date as calendar week"),
        "internalComment": _property("string", "Internal comment"),
        "customerProductNumber": _property("string", "Customer product number"),
        "countryOfOrigin": _property("string", "Country of origin"),
        "hsCode": _property("string", "HS code"),
        "externalNumber": _property("string", "External number"),
        "hasChildLineItems": _property("boolean", "Has child line items", access="readOnly"),
        "parentLineItem": _property(
            "reference", "Parent line item", reference="LineItem", renderProperty="id"
        ),
        "salesOrderLineItem": _property(
            "reference", "Sales order line item", reference="LineItem", renderProperty="id"
        ),
        "packagingUnit": _property("string", "Packaging unit", access="readOnly"),
        "customFields": _property(
            "collection",
            "Custom fields",
            access="readOnly",
            node={
                "properties": {
                    "key": _property("string", "Key", access="readOnly"),
                    "label": _property("string", "Label", access="readOnly"),
                    "value": _property("string", "Value", access="readOnly"),
                }
            },
        ),
        "taxRate": _property("string", "Tax rate"),
        "effectiveTaxRate": _property("decimal", "Effective tax rate"),
        "price": _property(
            "embedded",
            "Price",
            properties={
                "net": _property("embedded", "Net"),
                "gross": _property("embedded", "Gross"),
            },
        ),
        "discount": _property("decimal", "Discount"),
        "itemRevenue": _property(
            "embedded",
            "Item revenue",
            properties={
                "net": _property("embedded", "Net"),
                "gross": _property("embedded", "Gross"),
            },
        ),
        "lineItemRevenue": _property(
            "embedded",
            "Line item revenue",
            properties={
                "net": _property("embedded", "Net"),
                "gross": _property("embedded", "Gross"),
            },
        ),
        "purchasePrice": _property(
            "embedded",
            "Purchase price",
            properties={"net": _property("embedded", "Net")},
        ),
        "contributionMargin": _property("decimal", "Contribution margin"),
        "taxLegalNotice": _property("string", "Tax legal notice"),
        "printSettings": _property(
            "embedded",
            "Print settings",
            properties={
                "hidden": _property("boolean", "Hide on PDF"),
                "withoutPrice": _property("boolean", "Without prices"),
            },
        ),
        "createdAt": _property("datetime", "Created at", access="readOnly"),
        "updatedAt": _property("datetime", "Updated at", access="readOnly"),
    }


# Business-friendly filter keys shared by every v3 business document. The base
# schema renames the underlying references (``address`` → ``businessPartnerId``,
# ``project`` → ``projectId``, ``sales`` → ``salesId``) and exposes a flat
# ``country`` overview column, but the v3 list endpoint filters on the original
# nested keys — so these aliases are needed on every document or the renamed
# filter is rejected (400). Applied under each adapter's own ``query_aliases``
# (which win on conflict). Harmless where a field is not filterable on a given
# document (the alias is simply never used).
_BASE_DOC_FILTER_ALIASES: dict[str, str] = {
    "businessPartnerId": "address.id",
    "projectId": "project.id",
    "salesId": "sales.id",
    "country": "documentAddress.country",
}

# Filter operators the v3 document list endpoints accept, by resolved field kind
# (probed live). Surfaced per filterable field as ``operators`` so a caller picks
# a valid operator instead of guessing — a wrong name is sometimes rejected (400)
# and sometimes silently ignored (returns the full set), so guessing is unsafe.
_STRING_FILTER_OPS = [
    "equals",
    "notEquals",
    "in",
    "notIn",
    "contains",
    "notContains",
    "startsWith",
    "endsWith",
    "isNull",
    "isNotNull",
]
# Numbers and dates: comparison operators (note the plural ``…OrEquals``). The v3
# endpoints ALSO advertise ``lessThan`` / ``lessThanOrEquals`` but SILENTLY IGNORE
# them (an upper-bound filter returns the full, unfiltered set — verified live;
# the lower-bound greaterThan on the same field narrows correctly), so they are
# deliberately omitted to keep an "up to X" filter from being silently wrong.
_RANGE_FILTER_OPS = [
    "equals",
    "notEquals",
    "greaterThan",
    "greaterThanOrEquals",
    "isNull",
    "isNotNull",
]
# References / ids: exact / set membership only — no substring.
_ID_FILTER_OPS = ["equals", "notEquals", "in", "notIn", "isNull", "isNotNull"]


class BusinessDocumentAdapterBase:
    manifest: EmulationManifest
    base_path: str = ""
    list_include: str = "customFields,project,address,tags"
    detail_include: str = "lineItems,lineItems.product,lineItems.customFields,customFields,project,address,tags,activity"
    payload_read_only_fields: set[str] = {
        "uuid",
        "id",
        "documentNumber",
        "documentStatus",
        "isWriteProtected",
        "isDocumentSent",
        "customerNumber",
        "effectiveAddresses",
        "createdAt",
        "updatedAt",
    }
    payload_field_map: dict[str, str] = {}
    payload_object_fields: set[str] = {"address", "project", "sales", "salesOrder", "invoice"}
    query_aliases: dict[str, str] = {}
    # Root filter fields to un-mark as ``filterable`` for this document — used
    # where the base marks a field filterable but the entity's v3 endpoint has
    # no matching filter key (advertising it would only yield a 400). Verified
    # per entity against the live allow-list.
    filterable_removals: tuple[str, ...] = ()
    line_item_create_fields: set[str] = {
        "product",
        "quantity",
        "name",
        "description",
        "countryOfOrigin",
        "hsCode",
        "deliveryDate",
        "deliveryDateAsCalendarWeek",
        "internalComment",
        "customerProductNumber",
        "unit",
        "printSettings",
        "deliveredQuantity",
        "externalNumber",
    }
    line_item_update_fields: set[str] = {
        "name",
        "description",
        "quantity",
        "deliveryDate",
        "deliveryDateAsCalendarWeek",
        "internalComment",
        "customerProductNumber",
        # countryOfOrigin + hsCode: create-only historically; API-725 (mvp 26.30.1)
        # accepts them on the line-item UPDATE too. Verified live against mvp.
        "countryOfOrigin",
        "hsCode",
        "sort",
        "unit",
        "printSettings",
        "deliveredQuantity",
        "externalNumber",
    }
    root_property_aliases: dict[str, str] = {}
    line_item_property_aliases: dict[str, str] = {
        "product": "productId",
        "sort": "order",
        "deliveryDateAsCalendarWeek": "shouldShowDeliveryDateAsCalendarWeek",
        "unit": "legacyUnitOfMeasure",
        "parentLineItem": "parentLineItemId",
        "salesOrderLineItem": "salesOrderLineItemId",
    }
    csv_root_properties: dict[str, dict[str, Any]] = {}
    csv_line_item_properties: dict[str, dict[str, Any]] = {}
    legacy_root_field_map: dict[str, str] = {}
    legacy_line_item_field_map: dict[str, str] = {}
    payload_root_field_map: dict[str, str] = {}
    payload_line_item_field_map: dict[str, str] = {}
    # --- Action capabilities, grounded per entity in the real Xentral
    # *ActionsController. Lifecycle/status transitions surfaced in the
    # documentStatus process step (and executable via action()); the standard
    # send/write-protection/activity actions; and the two SalesOrder-only legacy
    # v1-beta fulfillment actions. Defaults match the eight v3 business
    # documents — adapters that lack an action (production, price inquiry)
    # switch the relevant flag off.
    lifecycle_actions: tuple[str, ...] = ()
    release_has_document_date: bool = True
    supports_send: bool = True
    supports_write_protection: bool = True
    supports_log_activity: bool = True
    supports_dispatch: bool = False
    supports_create_partial_sales_order: bool = False
    # Whether the document carries the v3 ``tags`` field (CreateBusinessDocumentData
    # base). Enables the addTag / removeTag actions, applied via read-modify-write
    # on the main endpoint. Defaults true for business documents.
    supports_tags: bool = True
    preview_property_names: tuple[str, ...] = (
        "documentNumber",
        "documentStatus",
        "address",
        "customerNumber",
        "documentDate",
        "deliveryDate",
        "totalGrossAmount",
        "currency",
        "paymentStatus",
    )

    def metadata(self, accept_language: str | None = None) -> dict[str, Any]:
        properties = self._aliased_properties(self._root_properties(), self.root_property_aliases)
        self._apply_previewable_columns(properties)
        for removal in self.filterable_removals:
            if removal in properties and isinstance(properties[removal], dict):
                properties[removal].pop("filterable", None)
        self._annotate_filter_operators(properties)
        meta: dict[str, Any] = {
            "key": self.manifest.key,
            "label": self.manifest.label("en"),
            "operations": list(self.manifest.operations),
            "previewTemplateString": "{{documentNumber}}",
            "sections": self._sections(),
            "rootNode": {"properties": properties},
            "origin": "emulated",
            "emulation": self.manifest.marker(),
        }
        # Two optional invokable surfaces beyond CRUD — only emitted when a
        # subclass declares them, so the wire stays lean for plain documents:
        #   actions      — free entity actions (e.g. printDocument)
        #   processSteps — lifecycle/status commands grouped by process step
        #                  (e.g. ReleaseDocumentCommand / CancelDocumentCommand)
        # Both execute via PATCH /api/entity/<key>/actions/<key> with the BF
        # {ids, command} envelope; the workflow node surfaces them generically.
        actions = self._actions()
        if actions:
            meta["actions"] = actions
        process_steps = self._process_steps()
        if process_steps:
            meta["processSteps"] = process_steps
        return meta

    def _annotate_filter_operators(self, properties: dict[str, Any]) -> None:
        """Tag each filterable field with the operators its v3 filter key accepts,
        so a caller need not guess. A field that resolves to a ``.id`` key (or is a
        reference) takes the id set; dates/numbers take the comparison set;
        everything else (text, enum, tag) is text. documentAddress sub-fields are
        text. Idempotent — safe to call again after adding more filterable fields."""
        aliases = {**_BASE_DOC_FILTER_ALIASES, **self.query_aliases}

        def ops_for(name: str, spec: dict[str, Any]) -> list[str]:
            resolved = aliases.get(name, name)
            ptype = str(spec.get("type") or "string").lower()
            if resolved.endswith(".id") or ptype in (
                "reference",
                "relation",
                "entity",
                "uuid",
                "id",
            ):
                return list(_ID_FILTER_OPS)
            if ptype in ("date", "datetime", "decimal", "integer", "number", "float"):
                return list(_RANGE_FILTER_OPS)
            return list(_STRING_FILTER_OPS)

        for name, spec in properties.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("filterable"):
                spec["operators"] = ops_for(name, spec)
            if name == "documentAddress":
                for sub in (spec.get("properties") or {}).values():
                    if isinstance(sub, dict) and sub.get("filterable"):
                        sub["operators"] = list(_STRING_FILTER_OPS)

    def _preview_property_names(self) -> tuple[str, ...]:
        return self.preview_property_names

    def _apply_previewable_columns(self, properties: dict[str, Any]) -> None:
        """Treat previewable as the curated table overview, not a property hint.

        Individual property builders still set previewable for readability, but
        the final schema gets one explicit list so every document overview is
        predictable and role-oriented.
        """
        for spec in properties.values():
            spec.pop("previewable", None)
        names = self._preview_property_names()
        for idx, key in enumerate(names):
            if key in properties:
                properties[key]["previewable"] = True
                properties[key]["previewOrder"] = idx
        # The document-stored name and country are universally useful overview
        # columns, surfaced on every document that carries an address so they all
        # look the same up here. The name sits right after the businessPartnerId
        # reference (the readable counterpart to that id); country goes last.
        if "documentAddress" in properties:
            partner_order = properties.get("businessPartnerId", {}).get("previewOrder")
            if "documentAddressName" in properties and not properties["documentAddressName"].get(
                "previewable"
            ):
                properties["documentAddressName"]["previewable"] = True
                properties["documentAddressName"]["previewOrder"] = (
                    partner_order + 0.5 if partner_order is not None else len(names)
                )
            if "country" in properties and not properties["country"].get("previewable"):
                properties["country"]["previewable"] = True
                properties["country"]["previewOrder"] = len(names) + 1

    @classmethod
    def _send_to_recipient_command_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recipientEmail": {"type": "string", "label": "Recipient email"},
                "recipientName": {"type": "string", "label": "Recipient name"},
                "subject": {"type": "string", "label": "Subject"},
                "body": {"type": "string", "label": "Body"},
                "cc": {"type": "string", "label": "CC"},
                "bcc": {"type": "string", "label": "BCC"},
                "printerId": {"type": "string", "label": "Printer ID"},
                "printerName": {"type": "string", "label": "Printer name"},
                "markAsSent": {"type": "boolean", "label": "Mark document as sent"},
            },
        }

    def _send_to_recipient_action(self) -> dict[str, Any]:
        return {
            "key": "sendToRecipient",
            "label": "Send to recipient",
            "bulk": False,
            "method": "PATCH",
            "path": f"/api/entity/{self.manifest.key}/actions/sendToRecipient",
            "destructive": True,
            "description": "Send this document to its recipient using the document's Xentral send workflow.",
            "command": self._send_to_recipient_command_schema(),
        }

    def _actions(self) -> list[dict[str, Any]]:
        """Free entity actions, derived from the capability flags. ``send`` maps
        to ``sendToRecipient``; write protection, activity logging and the
        partial-order creator come from the shared builder."""
        actions: list[dict[str, Any]] = []
        if self.supports_send:
            actions.append(self._send_to_recipient_action())
        actions.extend(build_document_actions(self))
        return actions

    def _action_payload(self, command: dict[str, Any]) -> dict[str, Any]:
        email: dict[str, Any] = {}
        email_map = {
            "recipientEmail": "to",
            "recipientName": "name",
            "subject": "subject",
            "body": "body",
        }
        for source, target in email_map.items():
            value = command.get(source)
            if value not in (None, ""):
                email[target] = deepcopy(value)
        for key in ("cc", "bcc"):
            value = command.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, str):
                email[key] = [item.strip() for item in value.split(",") if item.strip()]
            else:
                email[key] = deepcopy(value)
        if email:
            return {"email": email}

        printer: dict[str, Any] = {}
        for source, target in (("printerId", "id"), ("printerName", "name")):
            value = command.get(source)
            if value not in (None, ""):
                printer[target] = deepcopy(value)
        if printer:
            return {"printer": printer}

        if command.get("markAsSent") is True:
            return {"markAsSent": True}
        return {}

    def _process_steps(self) -> list[dict[str, Any]]:
        """Lifecycle/status commands grouped by process step, derived from
        ``lifecycle_actions`` (+ the SalesOrder-only ``dispatch`` transition).
        Grounded per entity via the capability flags."""
        return build_process_steps(self)

    @staticmethod
    def _aliased_properties(
        properties: dict[str, Any],
        aliases: dict[str, str],
    ) -> dict[str, Any]:
        if not aliases:
            return properties
        result: dict[str, Any] = {}
        for key, value in properties.items():
            result[aliases.get(key, key)] = value
        return result

    def _sections(self) -> dict[str, Any]:
        return {
            "general": {"label": "General"},
            "address": {"label": "Address"},
            "references": {"label": "References"},
            "shipping": {"label": "Shipping"},
            "financials": {"label": "Financials"},
            "dunning": {"label": "Dunning"},
            "content": {"label": "Content"},
            "lineItems": {"label": "Line items"},
            "customFields": {"label": "Custom Fields"},
        }

    def _root_properties(self) -> dict[str, Any]:
        p = lambda type_, *labels, **extra: _property(type_, *labels, **extra)  # noqa: E731
        props: dict[str, Any] = {
            "uuid": p("string", "UUID", "UUID", access="readOnly"),
            "id": p("string", "ID", "ID", access="readOnly"),
            "address": p(
                "reference",
                "Business partner",
                reference="BusinessPartner",
                renderProperty="name",
                section="general",
                filterable=True,
                previewable=True,
                rules=["required"],
            ),
            "documentNumber": p(
                "string",
                "Document number",
                access="readOnly",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            "documentStatus": p(
                "select",
                "Status",
                "Status",
                access="readOnly",
                filterable=True,
                previewable=True,
                section="general",
            ),
            "documentDate": p(
                "date",
                "Document date",
                filterable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            "isWriteProtected": p(
                "boolean", "Write protected", access="readOnly", section="general"
            ),
            "isDocumentSent": p("boolean", "Document sent", access="readOnly", section="general"),
            "customerNumber": p(
                "string",
                "Customer number",
                access="readOnly",
                filterable=True,
                searchable=True,
                sortable=True,
                previewable=True,
                section="general",
            ),
            "documentAddress": p(
                "embedded",
                "Document address",
                section="address",
                properties=_postal_address_properties(),
            ),
            # Flat overview columns filled from documentAddress in _entity_record
            # so the table can show the name/country the document itself stored,
            # without digging into the embedded address. Distinct from the
            # businessPartnerId reference (which points at the master partner).
            "documentAddressName": p("string", "Name", access="readOnly", section="address"),
            "country": p(
                "string", "Country", access="readOnly", filterable=True, section="address"
            ),
            "project": p(
                "reference",
                "Project",
                reference="Project",
                renderProperty="name",
                filterable=True,
                section="references",
            ),
            "sales": p(
                "reference",
                "Sales",
                reference="BusinessPartner",
                renderProperty="name",
                filterable=True,
                section="references",
            ),
            "masterReferenceNumber": p(
                "string",
                "Master reference number",
                filterable=True,
                section="references",
            ),
            "costCenterValue": p("string", "Cost center", filterable=True, section="references"),
            "language": p("string", "Language", section="content"),
            "internalDesignation": p("string", "Internal designation", section="content"),
            "bodyIntroduction": p("string", "Body introduction", section="content"),
            "bodyOutroduction": p("string", "Body outroduction", section="content"),
            "deliveryTerms": p("string", "Delivery terms", section="shipping"),
            "internalComment": p("string", "Internal comment", section="content"),
            "printSettings": p(
                "embedded",
                "Print settings",
                section="content",
                properties={
                    "withoutLetterhead": p("boolean", "Without letterhead"),
                    "withoutProductText": p("boolean", "Without product text"),
                },
            ),
            # Tags ("Labels") are filterable via the V3 list endpoint
            # (filter[tags] / filter[tags.id]); the query pass-through forwards
            # the filter unchanged, so only the discovery flag is needed here.
            "tags": p("tag", "Tags", section="content", filterable=True),
            "customFields": p(
                "collection",
                "Custom Fields",
                access="readOnly",
                section="customFields",
                node={
                    "properties": {
                        "key": p("string", "Key", access="readOnly"),
                        "label": p("string", "Label", access="readOnly"),
                        "value": p("string", "Value", access="readOnly"),
                    }
                },
            ),
            "editor": p(
                "reference",
                "Editor",
                reference="BusinessPartner",
                renderProperty="name",
                section="general",
            ),
            "lineItems": p(
                "collection",
                "Line items",
                section="lineItems",
                node={"properties": self._line_item_properties()},
            ),
            "createdAt": p("datetime", "Created at", access="readOnly"),
            "updatedAt": p("datetime", "Updated at", access="readOnly"),
        }
        props.update(self._extra_root_properties())
        props.update(self._csv_properties(self.csv_root_properties))
        return props

    def _extra_root_properties(self) -> dict[str, Any]:
        return {}

    def _line_item_properties(self) -> dict[str, Any]:
        return self._aliased_properties(
            {
                **_base_line_item_properties(),
                **self._extra_line_item_properties(),
                **self._csv_properties(self.csv_line_item_properties),
            },
            self.line_item_property_aliases,
        )

    @staticmethod
    def _csv_properties(specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        props: dict[str, dict[str, Any]] = {}
        for key, spec in specs.items():
            spec = dict(spec)
            type_ = spec.pop("type", "string")
            label = spec.pop("label", key[:1].upper() + key[1:])
            props[key] = _property(type_, label, **spec)
        return props

    def _extra_line_item_properties(self) -> dict[str, Any]:
        return {}

    def _record_transform(self, record: dict[str, Any]) -> dict[str, Any]:
        return record

    def _payload_nested(self, key: str, value: Any) -> dict[str, Any] | None:
        return None

    def _line_item_transform(self, item: dict[str, Any]) -> dict[str, Any]:
        original = deepcopy(item)
        if item.get("id") is not None:
            item["id"] = str(item["id"])
            item["uuid"] = item["id"]
        if "order" in item and "sort" not in item:
            item["sort"] = item.pop("order")
        product = item.get("product")
        if isinstance(product, dict) and product.get("id") is not None:
            product["id"] = str(product["id"])
        item = self._aliased_record(item, self.line_item_property_aliases)
        for legacy_key, property_key in self.legacy_line_item_field_map.items():
            if legacy_key in original and property_key not in item:
                item[property_key] = original[legacy_key]
        return item

    @staticmethod
    def _aliased_record(record: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
        if not aliases:
            return record
        for old_key, new_key in aliases.items():
            if old_key in record and new_key not in record:
                record[new_key] = record.pop(old_key)
        return record

    def _line_item_payload(self, item: Any, *, creating: bool) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Each lineItems entry must be an object")
        allowed = self.line_item_create_fields if creating else self.line_item_update_fields
        payload = {key: deepcopy(value) for key, value in item.items() if key in allowed}
        inverse_aliases = {new: old for old, new in self.line_item_property_aliases.items()}
        for external_key, internal_key in inverse_aliases.items():
            if external_key in item and internal_key in allowed and internal_key not in payload:
                payload[internal_key] = deepcopy(item[external_key])
        for external_key, internal_key in self.payload_line_item_field_map.items():
            if external_key in item and internal_key not in payload:
                payload[internal_key] = deepcopy(item[external_key])
        product = payload.get("product")
        if product not in (None, "") and not isinstance(product, dict):
            payload["product"] = {"id": str(product)}
        sales_order_line_item = payload.get("salesOrderLineItem")
        if sales_order_line_item not in (None, "") and not isinstance(sales_order_line_item, dict):
            payload["salesOrderLineItem"] = {"id": str(sales_order_line_item)}
        parent_line_item = payload.get("parentLineItem")
        if parent_line_item not in (None, "") and not isinstance(parent_line_item, dict):
            payload["parentLineItem"] = {"id": str(parent_line_item)}
        return payload

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
            "country": source.get("country"),
            "vatId": vat_id,
        }

    @staticmethod
    def _postal_address_to_v3(value: Any) -> tuple[dict[str, Any] | None, Any]:
        if value is None:
            return None, None
        if not isinstance(value, dict):
            raise ValueError("documentAddress must be an object or null")
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

    def _query(self, query: list[tuple[str, str]]) -> list[tuple[str, str]]:
        # Shared document filter aliases with this adapter's own on top (adapter
        # wins on conflict), so the renamed reference/overview filter keys map to
        # their real v3 keys on every document.
        aliases = {**_BASE_DOC_FILTER_ALIASES, **self.query_aliases}
        translated: list[tuple[str, str]] = []
        for key, value in query:
            lookup = key[:-6] if key.endswith("[key]") else key
            if key.endswith("[key]"):
                value = aliases.get(value, value)
            elif key == "sort":
                prefix = "-" if value.startswith("-") else ""
                sort_key = value[1:] if prefix else value
                value = prefix + aliases.get(sort_key, sort_key)
            elif lookup in aliases:
                value = aliases[lookup] if value == "" else value
            translated.append((key, value))
        return translated

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

    async def _sync_line_items(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        desired: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> httpx.Response | None:
        current_response = await client.get(url, params={"include": "lineItems"}, headers=headers)
        if current_response.status_code >= 400:
            return current_response
        try:
            current_document = current_response.json().get("data", {})
        except (AttributeError, ValueError):
            return current_response

        existing_items = current_document.get("lineItems", [])
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
                    "title": f"Invalid {self.manifest.key} line item",
                    "detail": f"Unknown line item id(s): {missing}",
                },
            )

        for line_item_id in existing_ids - desired_ids:
            response = await client.delete(f"{url}/lineItems/{line_item_id}", headers=headers)
            if response.status_code >= 400:
                return response

        for item in desired:
            line_item_id = item.get("id")
            if line_item_id is None:
                payload = self._line_item_payload(item, creating=True)
                response = await client.post(f"{url}/lineItems", json=payload, headers=headers)
            else:
                payload = self._line_item_payload(item, creating=False)
                if not payload:
                    continue
                response = await client.patch(
                    f"{url}/lineItems/{line_item_id}",
                    json=payload,
                    headers=headers,
                )
            if response.status_code >= 400:
                return response
        return None

    def _v3_payload(
        self, body: bytes | None, *, line_items_creating: bool = True
    ) -> dict[str, Any]:
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        out: dict[str, Any] = {}
        inverse_aliases = {new: old for old, new in self.root_property_aliases.items()}
        for key, value in payload.items():
            key = inverse_aliases.get(key, key)
            if key in self.payload_read_only_fields:
                continue
            nested = self._payload_nested(key, value)
            if nested is not None:
                out.update(nested)
                continue
            target = self.payload_root_field_map.get(key, self.payload_field_map.get(key, key))
            if (
                target in self.payload_object_fields
                and value not in (None, "")
                and not isinstance(value, dict)
            ):
                value = {"id": str(value)}
            out[target] = value

        if "lineItems" in out:
            if not isinstance(out["lineItems"], list):
                raise ValueError("lineItems must be an array")
            if line_items_creating:
                out["lineItems"] = [
                    self._line_item_payload(item, creating=True) for item in out["lineItems"]
                ]
            else:
                if not all(isinstance(item, dict) for item in out["lineItems"]):
                    raise ValueError("Each lineItems entry must be an object")
        return out

    @classmethod
    def _entity_record(cls, raw: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(raw)
        original = deepcopy(raw)
        entity_id = record.get("id")
        record["id"] = str(entity_id) if entity_id is not None else None
        record["uuid"] = record["id"]
        adapter = cls()
        record = adapter._record_transform(record)
        record = adapter._aliased_record(record, adapter.root_property_aliases)
        # Surface the name and country stored ON the document (its documentAddress
        # snapshot) as their own flat overview columns. `businessPartnerId` stays
        # a pure reference to the master partner — kept uniform across every
        # document — while these columns show what the document itself recorded.
        doc_addr = record.get("documentAddress")
        if isinstance(doc_addr, dict):
            if doc_addr.get("name") and not record.get("documentAddressName"):
                record["documentAddressName"] = doc_addr["name"]
            if doc_addr.get("country") and not record.get("country"):
                record["country"] = doc_addr["country"]
        for legacy_key, property_key in adapter.legacy_root_field_map.items():
            if legacy_key in original and property_key not in record:
                record[property_key] = original[legacy_key]
        allowed = set(adapter.metadata("en")["rootNode"]["properties"])
        return {key: value for key, value in record.items() if key in allowed}

    def _forward_headers(self, response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key in (
                "content-type",
                "content-disposition",
                "etag",
                "cache-control",
                "x-pagination",
                "location",
            )
            if (value := response.headers.get(key))
        }

    @classmethod
    def _json_response(
        cls,
        status_code: int,
        data: dict[str, Any],
        source_headers: httpx.Headers | None = None,
    ) -> AdapterResponse:
        headers = {"content-type": "application/json"}
        if source_headers is not None:
            headers.update(
                {
                    key: value
                    for key in ("etag", "cache-control", "x-pagination")
                    if (value := source_headers.get(key))
                }
            )
        return AdapterResponse(
            status_code=status_code,
            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )

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
        path = self.base_path
        if handle:
            if not handle.isdigit():
                return self._json_response(
                    400,
                    {
                        "title": f"Invalid {self.manifest.key} handle",
                        "detail": "Expected the numeric V3 id.",
                    },
                )
            path = f"{path}/{handle}"

        params = self._query(query)
        if method == "GET":
            if not any(key == "include" for key, _ in params):
                params.append(("include", self.detail_include if handle else self.list_include))

        request_body: dict[str, Any] | None = None
        if method in {"POST", "PATCH", "PUT"}:
            try:
                request_body = self._v3_payload(body, line_items_creating=method == "POST")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return self._json_response(
                    400,
                    {"title": f"Invalid {self.manifest.key} payload", "detail": str(exc)},
                )

        headers = self._request_headers(token, accept_language)

        async def _perform(request_client: httpx.AsyncClient) -> httpx.Response:
            url = f"{base_url.rstrip('/')}{path}"
            if (
                method in {"PATCH", "PUT"}
                and handle
                and request_body is not None
                and "lineItems" in request_body
            ):
                desired_line_items = request_body.pop("lineItems")
                if request_body:
                    response = await request_client.request(
                        method,
                        url,
                        params=params,
                        json=request_body,
                        headers=headers,
                    )
                    if response.status_code >= 400:
                        return response
                sync_error = await self._sync_line_items(
                    request_client,
                    url=url,
                    desired=desired_line_items,
                    headers=headers,
                )
                if sync_error is not None:
                    return sync_error
                return await request_client.get(
                    url,
                    params={"include": self.detail_include},
                    headers=headers,
                )
            return await request_client.request(
                method,
                url,
                params=params,
                json=request_body,
                headers=headers,
            )

        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as request_client:
                response = await _perform(request_client)
        else:
            response = await _perform(client)

        if response.status_code >= 400 or not response.content:
            return AdapterResponse(
                response.status_code, response.content, self._forward_headers(response)
            )

        try:
            data = response.json()
        except ValueError:
            return AdapterResponse(
                response.status_code, response.content, self._forward_headers(response)
            )

        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data["data"] = [
                self._entity_record(row) if isinstance(row, dict) else row for row in data["data"]
            ]
        elif isinstance(data, dict) and isinstance(data.get("data"), dict):
            data["data"] = self._entity_record(data["data"])
        elif isinstance(data, dict):
            data = self._entity_record(data)

        return self._json_response(response.status_code, data, response.headers)

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
        return await execute_document_action(
            self,
            action_key=action_key,
            body=body,
            base_url=base_url,
            token=token,
            accept_language=accept_language,
            client=client,
        )
