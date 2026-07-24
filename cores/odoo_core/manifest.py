"""Odoo — a dynamic live-passthrough core over an Odoo instance (mode C2).

Odoo speaks JSON-RPC, not our wire dialect, so this core *translates* at the
boundary — but nothing is modeled by hand: the entity roster comes live from
``ir.model``, the field contract from ``fields_get``, and create/update are
offered per model according to the connected user's access rights (see
``emulated/entities.py``). A tenant's custom modules and fields surface
automatically. Connection config (URL / db / login / API key) is resolved per
tenant from the vault (see ``emulated.base.ODOO_FIELDS``); nothing is
hardcoded. ``EmulatedOnly`` — the catalogue shows only the Odoo entities.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly
from .emulated import build_adapters

CORE = CoreManifest(
    id="odoo_core",
    label_de="Odoo",
    label_en="Odoo",
    order=20,
    # No badge: it only echoed the card title (see the other cores — a badge is
    # for status like WIP/DEMO, not for repeating the name).
    badge=None,
    native_policy=EmulatedOnly(),
    # Resolved live per tenant (see build_adapters) — never at import time.
    adapters_factory=build_adapters,
    description_de=(
        "Live-Anbindung an deine Odoo-Instanz: alle Geschäftsobjekte kommen "
        "direkt aus Odoo — inklusive eigener Module und Felder. Anlegen und "
        "Bearbeiten richtet sich nach deinen Odoo-Berechtigungen."
    ),
    description_en=(
        "Live connection to your Odoo instance: every business object comes "
        "straight from Odoo — including custom modules and fields. Create and "
        "update follow your Odoo access rights."
    ),
)
