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
    # WIP: no entities yet (Phase-0 scaffold). Flip to featured once Phase 1 lands.
    badge="WIP",
    native_policy=EmulatedOnly(),
    # Resolved live per tenant (see build_adapters) — never at import time. Empty
    # until a connection is configured AND Phase 1 adapters exist.
    adapters_factory=build_adapters,
    description_de=(
        "Live-Anbindung an deine weclapp-Instanz: Geschäftsobjekte kommen direkt "
        "aus weclapp über die REST-API. Anlegen und Bearbeiten richtet sich nach "
        "den Rechten deines API-Tokens."
    ),
    description_en=(
        "Live connection to your weclapp instance: business objects come straight "
        "from weclapp via the REST API. Create and update follow your API token's "
        "permissions."
    ),
)
