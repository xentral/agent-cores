"""Xentral V3 facade · correspondence — die Kontakthistorie am Partner.

Reads and writes the next-generation entity API ``/api/entity/correspondence``
(the same endpoint the standalone ``xentral_crm`` MCP tool wrapped — this entity
replaces it, and ``xentral_email`` keeps creating the *email* entries on real
sends). Full CRUD, live-verified on mvp 2026-08-02.

Two upstream dialect quirks, both measured and both still true:

* pagination is ``limit``/``offset``, NOT ``page[number]``/``page[size]`` —
  translated in ``_get`` below.
* sorting takes the entity API's ``sort[0][key]``/``sort[0][direction]`` pair. A
  FLAT ``sort=createdAt`` answers 422 "Each sort must have a string key and
  direction", which this adapter previously read as "the endpoint has no sort" —
  so it stripped the key and both directions came back in the same order. It
  sorts fine; only the dialect was wrong.
* ``date`` filters take ``equals``/``notEquals`` only; range operators are
  refused.

**The kind vocabulary was wrong and is corrected here.** This model used to
advertise eight types, four of which (``follow_up``, ``appointment``, ``ticket``,
``document``) upstream rejects with "The selected type is invalid" and which no
record on the instance carries — they were carried over from the dissolved CRM
tool. Upstream's enum has exactly five (``email``, ``letter``, ``letter_fax``,
``phone``, ``note``), all five of which create successfully. ``letter_fax``,
which the old list omitted, is ``fax`` outward.

Creating an ``email`` entry here writes a LOG line; it sends nothing. A real send
goes through the email tool, which writes its own entry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref

_CU = {"creatable": True, "updatable": True}
# The model's vocabulary → upstream's. Only these five exist; see the module docstring.
_KIND_UP = {
    "email": "email",
    "letter": "letter",
    "fax": "letter_fax",
    "phone": "phone",
    "note": "note",
}
_KIND_DOWN = {v: k for k, v in _KIND_UP.items()}
_ADDRESS_LEAVES = ("name", "street", "contactPerson", "postalCode", "city", "country")


class CorrespondenceAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Correspondence",
        label_en="Correspondence",
        category="crm",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.correspondence",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/entity/correspondence"
    include = ""
    preview_template = "{{subject}}"
    query_aliases = {
        "customer": "recipientAddress",
        "kind": "type",
        "sender.partner": "senderAddress",
        "delivery.isSent": "isSent",
    }
    filter_value_maps = {"kind": _KIND_UP}
    bf_sort = True  # sort[0][key]/sort[0][direction], not a flat `sort`
    # `bf_sort` already suppresses the tiebreak; keep it off explicitly because a
    # second sort key on this endpoint is unverified.
    sort_tiebreak = None
    sections = {
        "general": {"label": "General"},
        "parties": {"label": "Parties"},
        "delivery": {"label": "Delivery"},
    }

    def _created_handle(self, resp: Any) -> Any:
        """This entity is addressed by ``uuid``; ``GET /{id}`` answers 404."""
        rec = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(rec, dict):
            rec = resp if isinstance(resp, dict) else {}
        return rec.get("uuid") or rec.get("id")

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
        # limit/offset dialect: translate the generic page params, then keep only
        # what this endpoint knows — it rejects unknown query keys.
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
            # Keep filters AND the sort pair; drop the rest — the endpoint
            # rejects keys it does not know.
            query = [(k, v) for k, v in query if k.startswith("filter[") or k.startswith("sort")]
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
            "kind": prop(
                "select",
                "Kind",
                **_CU,
                section="general",
                filterable=True,
                previewable=True,
                options=[
                    {"value": "email", "label": "Email"},
                    {"value": "letter", "label": "Letter"},
                    {"value": "fax", "label": "Fax"},
                    {"value": "phone", "label": "Phone call"},
                    {"value": "note", "label": "Note"},
                ],
                description=(
                    "Creating an `email` entry writes a LOG line and sends nothing "
                    "— a real send goes through the email tool."
                ),
            ),
            "customer": prop(
                "reference",
                "Customer",
                **_CU,
                reference="Customer",
                renderProperty="name",
                section="general",
                filterable=True,
                description="The partner this entry belongs to (upstream `recipientAddress`).",
            ),
            "subject": prop(
                "string",
                "Subject",
                **_CU,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "content": prop("string", "Content", **_CU, section="general"),
            "date": prop(
                "date",
                "Date",
                **_CU,
                section="general",
                filterable=True,
                description=(
                    "Filterable with equals/notEquals only — the upstream rejects "
                    "range operators on date. Defaults to today on create."
                ),
            ),
            "time": prop(
                "string",
                "Time",
                **_CU,
                section="general",
                description="`HH:MM:SS`. Defaults to now (Europe/Berlin) on create.",
            ),
            "internalDesignation": prop("string", "Internal designation", **_CU, section="general"),
            "editor": prop(
                "string",
                "Editor",
                **_CU,
                section="general",
                description=(
                    "The user who handled it. Upstream stores a bare user UUID "
                    "string here, not a reference."
                ),
            ),
            "project": prop(
                "reference",
                "Project",
                **_CU,
                reference="Project",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            "sender": prop(
                "embedded",
                "Sender",
                section="parties",
                properties={
                    "partner": prop(
                        "reference",
                        "Partner",
                        **_CU,
                        reference="Customer",
                        renderProperty="name",
                        filterable=True,
                    ),
                    "name": prop("string", "Name", **_CU),
                    "company": prop("string", "Company", **_CU, searchable=True),
                },
            ),
            "recipient": prop(
                "embedded",
                "Recipient",
                section="parties",
                properties={
                    "email": prop("string", "Email", **_CU),
                    "company": prop("string", "Company", **_CU),
                    "address": prop(
                        "embedded",
                        "Postal address",
                        properties={
                            "name": prop("string", "Name", **_CU),
                            "street": prop("string", "Street", **_CU),
                            "contactPerson": prop("string", "Contact person", **_CU),
                            "postalCode": prop("string", "Postal code", **_CU),
                            "city": prop("string", "City", **_CU),
                            "country": prop("string", "Country", **_CU),
                        },
                    ),
                },
            ),
            "delivery": prop(
                "embedded",
                "Delivery",
                section="delivery",
                properties={
                    "sendAs": prop("string", "Send as", **_CU),
                    "emailCc": prop("string", "Cc", **_CU),
                    "emailBcc": prop("string", "Bcc", **_CU),
                    "emailAddress": prop(
                        "string",
                        "Email address",
                        **_CU,
                        description=(
                            "A second address column upstream keeps beside "
                            "`recipient.email`; its role is undocumented and it is "
                            "empty on every record on the instance. Writable, and "
                            "mapped so a value written here is not silently lost."
                        ),
                    ),
                    "hasSignature": prop("boolean", "With signature", **_CU),
                    "isFax": prop("boolean", "Sent by fax", **_CU),
                    "isSent": prop("boolean", "Sent", **_CU, filterable=True),
                    "printer": prop("integer", "Printer id", **_CU),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    @staticmethod
    def _ref_id(value: Any) -> Any:
        rid = value.get("id") if isinstance(value, dict) else value
        return str(rid).split("_", 1)[-1] if rid else None

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        recipient = r.get("recipientAddress")
        rid = recipient.get("id") if isinstance(recipient, dict) else recipient
        sender = r.get("senderAddress")
        postal = r.get("recipientPostalAddress") or {}
        prj = r.get("project")
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
            "kind": _KIND_DOWN.get(r.get("type") or "", r.get("type")),
            "customer": ref(
                "cus_",
                rid,
                None,
                recipient.get("name") if isinstance(recipient, dict) else None,
                "customers",
            ),
            "subject": r.get("subject") or None,
            "content": r.get("content") or None,
            "date": r.get("date"),
            "time": r.get("time") or None,
            "internalDesignation": r.get("internalDesignation") or None,
            "editor": r.get("editor") or None,
            "project": ref(
                "prj_",
                prj.get("id") if isinstance(prj, dict) else prj,
                None,
                None,
                "projects",
            ),
            "sender": {
                "partner": ref(
                    "cus_",
                    sender.get("id") if isinstance(sender, dict) else sender,
                    None,
                    None,
                    "customers",
                ),
                "name": r.get("senderName") or None,
                "company": r.get("senderCompany") or None,
            },
            "recipient": {
                "email": r.get("recipientEmail") or None,
                "company": r.get("recipientCompany") or None,
                "address": {leaf: postal.get(leaf) or None for leaf in _ADDRESS_LEAVES},
            },
            "delivery": {
                "sendAs": r.get("sendAs") or None,
                "emailCc": r.get("emailCc") or None,
                "emailBcc": r.get("emailBcc") or None,
                "emailAddress": r.get("email") or None,
                "hasSignature": r.get("hasSignature"),
                "isFax": r.get("isFax"),
                "isSent": r.get("isSent"),
                "printer": r.get("printer"),
            },
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        wire: dict[str, Any] = {}
        rejected: set[str] = set()

        if "kind" in model:
            kind = str(model["kind"] or "").strip()
            if kind not in _KIND_UP:
                # Upstream answers "The selected type is invalid" — name the five
                # it accepts rather than pass a doomed value through.
                rejected.add("kind")
            else:
                wire["type"] = _KIND_UP[kind]
        if "customer" in model:
            wire["recipientAddress"] = {"id": self._ref_id(model["customer"]) or ""}
        for mine, theirs in (
            ("subject", "subject"),
            ("content", "content"),
            ("date", "date"),
            ("internalDesignation", "internalDesignation"),
            ("editor", "editor"),
        ):
            if mine in model:
                wire[theirs] = model[mine]
        if "time" in model:
            wire["time"] = self._normalize_time(model["time"])
        if "project" in model:
            pid = self._ref_id(model["project"])
            wire["project"] = {"id": pid} if pid else None

        sender = model.get("sender") or {}
        if "partner" in sender:
            sid = self._ref_id(sender["partner"])
            wire["senderAddress"] = {"id": sid} if sid else None
        if "name" in sender:
            wire["senderName"] = sender["name"]
        if "company" in sender:
            wire["senderCompany"] = sender["company"]

        recipient = model.get("recipient") or {}
        if "email" in recipient:
            wire["recipientEmail"] = recipient["email"]
        if "company" in recipient:
            wire["recipientCompany"] = recipient["company"]
        address = recipient.get("address") or {}
        if address:
            wire["recipientPostalAddress"] = {
                leaf: address[leaf] for leaf in _ADDRESS_LEAVES if leaf in address
            }
            rejected.update(f"recipient.address.{k}" for k in address if k not in _ADDRESS_LEAVES)

        delivery = model.get("delivery") or {}
        for mine, theirs in (
            ("sendAs", "sendAs"),
            ("emailCc", "emailCc"),
            ("emailBcc", "emailBcc"),
            ("emailAddress", "email"),
            ("hasSignature", "hasSignature"),
            ("isFax", "isFax"),
            ("isSent", "isSent"),
            ("printer", "printer"),
        ):
            if mine in delivery:
                wire[theirs] = delivery[mine]

        if creating:
            # A log entry without a moment is not a log entry; upstream requires
            # neither, so default both rather than write a dateless record.
            now = datetime.now(ZoneInfo("Europe/Berlin"))
            wire.setdefault("date", now.strftime("%Y-%m-%d"))
            wire.setdefault("time", now.strftime("%H:%M:%S"))
        return wire, rejected

    @staticmethod
    def _normalize_time(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) == 5 and text[2] == ":":
            return f"{text}:00"
        return text
