"""weclapp (native) — a generated 1:1 mirror of weclapp's own API (mode C2).

Unlike ``agentos_neo_weclapp`` (a curated AgentOS-Neo shape), this core mirrors
weclapp verbatim: entity and field names as weclapp names them, generated from the
OpenAPI spec (see ``generator.py`` + ``docs/00-concept.md``). It reuses the curated
core's engine and its weclapp connection — the tenant connects once, both cores
work. Read-only v1; grouped under Labs until verified against a live tenant.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly

from .entities import build_adapters

CORE = CoreManifest(
    id="weclapp_core",
    label_de="weclapp (nativ)",
    label_en="weclapp (native)",
    # After the raw passthrough cores (odoo 20); a native mirror is the same family.
    order=21,
    native_policy=EmulatedOnly(),
    # Experimental generated mirror — grouped under the selector's "Labs" section.
    labs=True,
    # Read-only v1: generated entities expose list/read only.
    read_only=True,
    # Generated from the OpenAPI spec at request time (static file, no tenant).
    adapters_factory=build_adapters,
    description_de=(
        "Roh-Spiegel deiner weclapp-Instanz: Entitäten und Felder mit weclapp-"
        "eigenen Namen, generiert aus der OpenAPI-Spec. Teilt sich die weclapp-"
        "Verbindung mit „AgentOS Neo (based on weclapp)“. Read-only."
    ),
    description_en=(
        "Faithful mirror of your weclapp instance: entities and fields with "
        "weclapp's own names, generated from the OpenAPI spec. Shares the weclapp "
        "connection with 'AgentOS Neo (based on weclapp)'. Read-only."
    ),
)
