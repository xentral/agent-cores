"""Xentral V3 facade · correspondence — the CRM tab on the customer record.

Reads/creates ``/api/entity/correspondence`` (BF entity API; the same endpoint
the standalone ``xentral_crm`` MCP tool wrapped — this entity replaces it, and
``xentral_email`` keeps creating the *email* entries on real sends). Verified
upstream contract (see the dissolved tool's service, mvp-tested):

  - filters: ``recipientAddress`` (numeric customer id), ``type``, ``date``
    (``equals``/``notEquals`` ONLY — the endpoint rejects range ops on date)
  - pagination: ``limit``/``offset`` — NOT page[number]/page[size]; translated
    in ``_get`` below
  - GET ``/{id}`` works (read); POST creates

Create policy (carried over from the tool): only the manually-loggable kinds —
``email`` (a LOG entry only; a real send goes through ``xentral_email``),
``letter``, ``note``, ``phone``, ``follow_up``, ``appointment``. ``ticket`` and
``document`` come from the helpdesk / document-flow integrations and are
read-only here (a create naming them answers 409 on the ``type`` field).
``date``/``time`` default to now (Europe/Berlin) when omitted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref

_TYPES = (
    "email",
    "phone",
    "letter",
    "note",
    "follow_up",
    "appointment",
    "ticket",
    "document",
)
_CREATABLE_TYPES = frozenset({"email", "letter", "note", "phone", "follow_up", "appointment"})


class CorrespondenceAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Correspondence",
        label_en="Correspondence",
        category="crm",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.correspondence",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create"),
    )
    v3_path = "/api/entity/correspondence"
    include = ""
    preview_template = "{{subject}}"
    query_aliases = {"customer": "recipientAddress"}
    # The endpoint has no server-side sort surface we have verified — never
    # append a tiebreak or forward a sort key.
    sort_tiebreak = None
    sections = {"general": {"label": "General"}}

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
        # limit/offset dialect: translate the generic page params and keep only
        # filters — the endpoint rejects unknown query keys (sort, searchTerm).
        if not handle:
            q = dict(query)
            try:
                page = max(1, int(q.get("page[number]") or "1"))
            except ValueError:
                page = 1
            try:
                size = max(1, min(100, int(q.get("page[size]") or "25")))
            except ValueError:
                size = 25
            query = [(k, v) for k, v in query if k.startswith("filter[")]
            query += [("limit", str(size)), ("offset", str((page - 1) * size))]
        return await super()._get(
            base_url,
            token,
            handle=handle,
            query=query,
            accept_language=accept_language,
            client=client,
        )

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "type": prop(
                "select",
                "Type",
                section="general",
                creatable=True,
                filterable=True,
                previewable=True,
                options=[{"value": v, "label": v.replace("_", " ").capitalize()} for v in _TYPES],
            ),
            "customer": prop(
                "reference",
                "Customer",
                reference="Customer",
                renderProperty="name",
                section="general",
                creatable=True,
                filterable=True,
            ),
            "subject": prop(
                "string", "Subject", section="general", creatable=True, previewable=True
            ),
            "content": prop("string", "Content", section="general", creatable=True),
            "date": prop(
                "date",
                "Date",
                section="general",
                creatable=True,
                filterable=True,
                description=(
                    "Filterable with equals/notEquals only — the upstream rejects "
                    "range operators on date. Defaults to today on create."
                ),
            ),
            "time": prop("string", "Time", section="general", creatable=True),
            "isSent": prop("boolean", "Sent", **RO, section="general"),
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
    ):
        # Create needs bespoke handling: the base re-reads by the POST
        # response's numeric ``id``, but this BF entity is fetched by UUID only
        # (GET /{id} 404s — the same F3 quirk the Tag adapter carries). The
        # response delivers both, so re-read by uuid and fall back to the raw
        # body if that ever stops holding.
        if method.upper() != "POST":
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
        import json as _json

        try:
            model = _json.loads(body or b"{}")
        except (ValueError, TypeError):
            return self._json(400, {"title": "invalid JSON body"})
        if not isinstance(model, dict):
            return self._json(400, {"title": "body must be a JSON object"})
        payload, rejected = self.map_write(model, creating=True)
        if rejected:
            return self._json(
                409,
                {
                    "title": (
                        f"{self.manifest.key}: fields not writable via the current Xentral API"
                    ),
                    "detail": (
                        "ticket/document entries come from their integrations, and a "
                        "real email send goes through the email tool — only the "
                        "manually loggable kinds can be created here."
                        if "type" in rejected
                        else "These fields are not part of the create contract."
                    ),
                    "fields": sorted(rejected),
                },
            )
        st, resp = await self._send(base_url, token, "POST", None, payload, accept_language, client)
        if st >= 400:
            return self._json(st, resp if isinstance(resp, dict) else {"title": "create failed"})
        created = resp.get("data") if isinstance(resp, dict) else None
        uuid = (created or {}).get("uuid")
        if uuid:
            _, rpayload = await self._get(
                base_url,
                token,
                handle=str(uuid),
                query=[],
                accept_language=accept_language,
                client=client,
            )
            rec = rpayload.get("data") if isinstance(rpayload, dict) else None
            if isinstance(rec, dict):
                return self._json(201, {"data": self.map_read(rec)})
        if isinstance(created, dict):
            return self._json(201, {"data": self.map_read(created)})
        return self._json(201, resp if isinstance(resp, dict) else {})

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        recipient = r.get("recipientAddress")
        rid = recipient.get("id") if isinstance(recipient, dict) else recipient
        return {
            "object": "correspondence",
            # BF entities are fetched by uuid (GET /{id} 404s, GET /{uuid} 200);
            # encode the uuid so the speaking id round-trips through `get` (F3,
            # same as the Tag adapter).
            "id": (
                f"cor_{r['uuid']}"
                if r.get("uuid")
                else (f"cor_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "type": r.get("type"),
            "customer": ref(
                "cus_",
                rid,
                None,
                recipient.get("name") if isinstance(recipient, dict) else None,
                "customers",
            ),
            "subject": r.get("subject"),
            "content": r.get("content"),
            "date": r.get("date"),
            "time": r.get("time"),
            "isSent": r.get("isSent"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        rejected: set[str] = set()
        ctype = str(model.get("type") or "").strip()
        if ctype and ctype not in _CREATABLE_TYPES:
            # ticket/document entries come from their integrations, and a real
            # email send goes through xentral_email — refuse the manual create.
            rejected.add("type")
        customer = model.get("customer")
        customer_id = customer.get("id") if isinstance(customer, dict) else customer
        if isinstance(customer_id, str) and "_" in customer_id:
            customer_id = customer_id.split("_", 1)[1]
        for key in model:
            if key not in {
                "object",
                "type",
                "customer",
                "subject",
                "content",
                "date",
                "time",
            }:
                rejected.add(key)
        if rejected:
            return {}, rejected
        now = datetime.now(ZoneInfo("Europe/Berlin"))
        time_value = str(model.get("time") or "").strip() or now.strftime("%H:%M:%S")
        if len(time_value) == 5 and time_value[2] == ":":
            time_value = f"{time_value}:00"
        payload: dict[str, Any] = {
            "type": ctype,
            "recipientAddress": {"id": str(customer_id or "")},
            "date": str(model.get("date") or "").strip() or now.strftime("%Y-%m-%d"),
            "time": time_value,
            "subject": model.get("subject") or "",
            "content": model.get("content") or "",
            "isSent": False,
            "isDeleted": False,
            "isFax": False,
        }
        return payload, set()
