"""Xentral V3 facade · costCenter — Kostenstellen (Konfigurations-Stammdaten).

SECOND UPSTREAM (docs/02-ist-analyse §2b): the BF entity API
``/api/entity/costCenter`` — one of the few next-generation entities that is
both rich enough and writable, so this is the first *settings* entity in the
core that is not read-only. Every document already carries a ``costCenter``
STRING (the number); this entity is the catalogue behind it, so an agent can
enumerate the valid numbers before booking a document against one.

Live-verified against mvp (2026-08-05), full round trip: POST 201 → GET 200 →
PATCH 200 with the changed value read back → DELETE 204 → GET 404.

The upstream contract, measured rather than read off the schema:

  * paging   ``page[number]`` / ``page[size]`` (the base's default dialect).
    ``limit``/``offset`` are silently IGNORED — they come back as the full
    unpaged collection with ``perPage: 15``.
  * sort     only ``createdAt`` / ``updatedAt``. ``number`` and ``description``
    are flagged ``searchable`` upstream but answer 422 "Property 'x' is not
    sortable", so neither is declared sortable here.
  * filter   ``equals`` / ``contains`` / ``notEquals`` on ``number`` and
    ``description``. ``startsWith`` and ``greaterThan`` answer 422. ``id`` and
    ``uuid`` are NOT filterable ("Property 'id' is not filterable").
  * search   the endpoint accepts ``searchTerm`` and then IGNORES it — a term
    matching exactly one row answers with all of them. Search therefore runs
    through the shared ``contains`` fan-out (base ``search_fields``), never
    upstream.
  * unknown query keys are accepted and ignored, so the base's
    undeclared-filter guard is what keeps an unfiltered collection from reading
    as a filtered one.

Two traps this adapter has to absorb:

1. ``GET /{numeric id}`` answers 404 ("Entity not found with uuid 1") — the BF
   read path is keyed by UUID (same shape as ``tag``). The speaking id is
   therefore built from ``uuid``. But the create read-back in
   ``_write_document`` re-reads by whatever ``data.id`` the POST returned, and
   that is the NUMERIC id — so ``_resolve_upstream_handle`` maps numeric → uuid
   (the ChannelAdapter pattern, X5). Without it every create would answer with
   the raw upstream row instead of the model shape.
2. A PATCH naming a readOnly property answers 200 and silently drops it (sending
   ``id`` leaves the id untouched). ``map_write`` therefore never forwards
   anything but the three writable fields.

Model vocabulary: upstream ``description`` is the cost centre's LABEL, not a
long text, so it is exposed as ``name`` — the shape every other master-data
entity in this core uses (``query_aliases`` maps it back for filters).
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import FacadeAdapterBase, REQUIRED, RO, prop

_CU: dict[str, Any] = {"creatable": True, "updatable": True}


class CostCenterAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="CostCenter",
        label_en="Cost center",
        category="settings",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.costCenter",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
        description=(
            "The instance's cost-centre catalogue. The costCenter field on "
            "documents (quotes, orders, invoices, purchase orders, …) is a plain "
            "STRING carrying the number from this catalogue — it is not "
            "validated against these records, so read the catalogue first to "
            "learn which numbers exist."
        ),
    )
    v3_path = "/api/entity/costCenter"
    include = ""
    preview_template = "{{name}}"
    bf_sort = True
    # Upstream calls the label "description"; the model calls it "name".
    query_aliases = {"name": "description"}
    # Nothing but createdAt/updatedAt sorts here, and a compound tiebreak is not
    # part of the BF sort shape — never append one.
    sort_tiebreak = None
    sections = {"general": {"label": "General"}}

    # Per-tenant {numeric id -> uuid}. Only ever consulted for a handle that is
    # not already a uuid: the ids we hand out are uuid-based, so the ordinary
    # `get` path never touches this. The create read-back does (see the module
    # docstring), and so does a caller who passes the numeric id by hand.
    _uuid_index: dict[str, dict[str, str]] = {}

    async def _build_uuid_index(
        self, base_url: str, token: str, accept_language: str | None, client
    ) -> dict[str, str]:
        _, payload = await self._get(
            base_url,
            token,
            handle=None,
            query=[("page[number]", "1"), ("page[size]", "250")],
            accept_language=accept_language,
            client=client,
        )
        rows = (payload.get("data") if isinstance(payload, dict) else None) or []
        idx = {
            str(r["id"]): str(r["uuid"])
            for r in rows
            if isinstance(r, dict) and r.get("id") is not None and r.get("uuid")
        }
        type(self)._uuid_index[base_url] = idx
        return idx

    async def _resolve_upstream_handle(
        self, handle: str, *, base_url: str, token: str, accept_language: str | None, client
    ) -> str:
        h = str(handle)
        if "-" in h:  # already a uuid — the normal path, no lookup needed
            return handle
        idx = type(self)._uuid_index.get(base_url)
        # Cold cache, or a record created after the index was built (exactly the
        # create read-back) — rebuild once before giving up.
        if idx is None or h not in idx:
            idx = await self._build_uuid_index(base_url, token, accept_language, client)
        return idx.get(h, handle)

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "number": prop(
                "string",
                "Number",
                **_CU,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "name": prop(
                "string",
                "Name",
                **REQUIRED,
                **_CU,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
            ),
            "internalNote": prop("string", "Internal note", **_CU, section="general"),
            # Not sortable upstream, but both ARE filterable (range ops included).
            "createdAt": prop("datetime", "Created at", **RO, filterable=True, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, filterable=True, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "costCenter",
            # BF is keyed by uuid (GET /{numeric id} 404s) — encode the uuid so
            # the speaking id round-trips through `get`.
            "id": (
                f"cc_{r['uuid']}"
                if r.get("uuid")
                else (f"cc_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "number": r.get("number"),
            "name": r.get("description"),
            "internalNote": r.get("internalNote") or None,
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    # ---- write mapping ---------------------------------------------------
    # Model key → upstream key. These three are the entity's whole writable
    # surface (`access: readWrite` on mvp); everything else is readOnly and a
    # PATCH naming it is dropped upstream WITHOUT an error, so it is refused here.
    _WRITABLE = {"number": "number", "name": "description", "internalNote": "internalNote"}
    # Echoed back by a read-modify-write caller — dropped, not refused.
    _IGNORE = {"object", "id", "uuid", "createdAt", "updatedAt"}

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        """Map the model onto the BF costCenter body.

        Membership is tested with ``in`` rather than a truthiness check so an
        explicit ``null`` still reaches upstream — clearing ``internalNote`` is a
        real edit, and dropping it would answer 200 having changed nothing.
        ``number`` and ``description`` are required on create and carry a
        ``filled`` rule, so upstream — not this mapping — rejects an empty one.
        """
        body: dict[str, Any] = {}
        rejected: set[str] = set()
        for key, value in model.items():
            if key in self._IGNORE:
                continue
            upstream = self._WRITABLE.get(key)
            if upstream is None:
                rejected.add(key)
                continue
            body[upstream] = value
        return body, rejected
