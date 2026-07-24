"""Xentral Business Entities — native Xentral, 1:1.

Exactly the entities the instance's Xentral returns via ``GET /api/metadata`` —
no emulators, no curation (``IncludeAll(curate=False)``). The pristine reference
core; hand-tune the emulated/curated set in ``xentral_api`` (or a new core)
instead of touching this one.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, IncludeAll

CORE = CoreManifest(
    id="xentral_business_entities",
    label_de="Xentral Business Entities",
    label_en="Xentral Business Entities",
    order=1,
    badge=None,
    native_policy=IncludeAll(curate=False),
    adapters=(),
    description_de=(
        "Deine Xentral-Instanz 1:1: jede Entität exakt so, wie die native API "
        "sie liefert — das vollständige, ungefilterte Datenmodell."
    ),
    description_en=(
        "Your Xentral instance 1:1: every entity exactly as the native API "
        "exposes it — the complete, unfiltered data model."
    ),
)
