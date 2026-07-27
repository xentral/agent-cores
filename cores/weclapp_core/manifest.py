"""weclapp Adapter — a generated 1:1 mirror of weclapp's own API (mode C2).

Unlike ``agentos_neo_weclapp`` (a curated AgentOS-Neo shape), this core mirrors
weclapp verbatim: entity and field names as weclapp names them, generated from the
OpenAPI spec (see ``generator.py`` + ``docs/00-concept.md``). It reuses the curated
core's engine and its weclapp connection — the tenant connects once, both cores
work. Offered as a regular core (no longer behind the selector's Labs
disclosure); read and write, with the verbs generated per entity from the spec.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly

from .entities import build_adapters

CORE = CoreManifest(
    id="weclapp_core",
    label_de="weclapp Adapter",
    label_en="weclapp Adapter",
    # After the raw passthrough cores (odoo 20); a native mirror is the same family.
    order=21,
    native_policy=EmulatedOnly(),
    # Read + write: create/update/delete are enabled per entity from the spec's path
    # verbs (see generator._operations). Not read-only, so no "read-only" marking.
    read_only=False,
    # Generated from the OpenAPI spec at request time (static file, no tenant).
    adapters_factory=build_adapters,
    description_de=(
        "Roh-Spiegel deiner weclapp-Instanz: Entitäten und Felder mit weclapp-"
        "eigenen Namen, generiert aus der OpenAPI-Spec. Anlegen/Bearbeiten/Löschen "
        "je Entität gemäß weclapp. Teilt sich die weclapp-Verbindung mit „AgentOS "
        "Neo (based on weclapp)“."
    ),
    description_en=(
        "Faithful mirror of your weclapp instance: entities and fields with "
        "weclapp's own names, generated from the OpenAPI spec. Create/update/delete "
        "per entity as weclapp allows. Shares the weclapp connection with 'AgentOS "
        "Neo (based on weclapp)'."
    ),
)
