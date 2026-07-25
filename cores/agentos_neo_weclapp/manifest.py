"""AgentOS Neo (weclapp) — a live-passthrough core over a weclapp instance (mode C2).

weclapp speaks a REST/JSON API (per-entity resource routes, an
``AuthenticationToken`` header), not our wire dialect, so this core *translates*
at the boundary — like ``odoo_core`` does for JSON-RPC. Unlike Odoo, weclapp has
no runtime schema-introspection API (no ``ir.model`` / ``fields_get``), so the
entity roster and field contract are NOT discovered live: they are curated /
generated from weclapp's OpenAPI spec (see ``docs/00-concept.md``). Connection
config (base URL + API token) is resolved per tenant from the vault (see
``emulated.base.WECLAPP_FIELDS``); nothing is hardcoded. ``EmulatedOnly`` — the
catalogue shows only the weclapp entities.

This is the **Phase-0 scaffold**: the connection + REST client foundation are in
place, but ``build_adapters`` returns no entities yet. The per-entity facade
adapters land in ``emulated/`` in Phase 1 (concept + phased plan in ``docs/``).
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly

from .emulated import build_adapters

CORE = CoreManifest(
    id="agentos_neo_weclapp",
    label_de="AgentOS Neo (based on weclapp)",
    label_en="AgentOS Neo (based on weclapp)",
    # After agentos_neo_xentral (8) but before the raw passthrough cores (odoo 20).
    order=9,
    native_policy=EmulatedOnly(),
    # Read-only core: every entity is list/read only (v1). The selector marks it
    # as a pure read core; the adapters' ``operations`` are the real gate.
    read_only=True,
    # Resolved live per tenant (see build_adapters) — never at import time.
    adapters_factory=build_adapters,
    description_de=(
        "Reiner Lese-Kern: Geschäftsobjekte werden live aus deiner weclapp-Instanz "
        "über die REST-API gelesen — Kunden, Artikel, Aufträge, Rechnungen und mehr. "
        "Anlegen und Bearbeiten ist (noch) nicht möglich."
    ),
    description_en=(
        "Read-only core: business objects are read live from your weclapp instance "
        "via the REST API — customers, articles, orders, invoices and more. Creating "
        "and editing is not (yet) supported."
    ),
)
