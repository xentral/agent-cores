"""Phoenix — a pure live-passthrough core over the Phoenix backend.

Phoenix already speaks the business-framework dialect natively (metadata
contract, ``/api/entity`` CRUD, BF query and action envelopes), so this core
models nothing: the entity roster, schemas, and data are all proxied 1:1 from
the live API. The roster is resolved per tenant at request time via
``adapters_factory`` (not baked in at import), so the catalogue always shows
exactly what that tenant's Phoenix currently serves. ``EmulatedOnly``.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly
from .emulated import build_adapters

CORE = CoreManifest(
    id="phoenix",
    label_de="Phoenix",
    label_en="Phoenix",
    order=10,
    # No badge: it only echoed the card title. Status badges (WIP/DEMO/…) are
    # reserved for cores that need to signal something the name doesn't.
    badge=None,
    native_policy=EmulatedOnly(),
    # Resolved live per tenant (see build_adapters) — never at import time.
    adapters_factory=build_adapters,
    description_de=(
        "Live-Anbindung an das Phoenix-Backend: Schema und Daten kommen direkt "
        "aus deiner Phoenix-Instanz — nichts wird lokal modelliert."
    ),
    description_en=(
        "Live connection to the Phoenix backend: schema and data come straight "
        "from your Phoenix instance — nothing is modeled locally."
    ),
)
