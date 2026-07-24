"""Xentral Basic — the hand-tuned core for the real ERP.

The emulated business objects, plus a small allowlist of native Xentral
entities that are already production-grade and best taken 1:1 rather than
emulated. Native entities not on the allowlist are dropped. This core owns its
full emulated-adapter set outright (see ``emulated/registry.py``) — it is fully
independent of any other core and pulls from no shared pool, so tuning here
never touches Classic. ``supplierInvoice`` is a Basic-local curated passthrough:
CRUD forwards to the native Business Entity API, while Basic owns the metadata
and tag actions.
"""

from __future__ import annotations

from entity_registry.core_sdk import Allowlist, CoreManifest
from .emulated import adapters

# Native (non-emulated) entity keys allowed in Basic when no local adapter with
# the same key supersedes them. Compared case-folded.
NATIVE_PASSTHROUGH_KEYS = frozenset({"supplierInvoice"})

# Xentral localizes native labels per Accept-Language (supplierInvoice → German
# "Eingangsrechnung" in a DE UI). Pin them to English so a passthrough entity
# reads in the same fixed style as every emulated entity in this core.
NATIVE_LABEL_OVERRIDES = {"supplierInvoice": "Supplier Invoice"}


CORE = CoreManifest(
    id="xentral_api",
    label_de="Xentral API based",
    label_en="Xentral API based",
    order=0,
    # Legacy: no longer offered in the ERP-Core selector. Stays vendored + is the
    # fallback default (DEFAULT_CORE_ID), so instances already on it keep working.
    hidden=True,
    badge=None,
    native_policy=Allowlist(NATIVE_PASSTHROUGH_KEYS),
    adapters=adapters(),
    native_label_overrides=NATIVE_LABEL_OVERRIDES,
    description_de=(
        "Der handkuratierte Kern für dein bestehendes Xentral-ERP: verfeinerte "
        "Geschäftsobjekte für den Hauptprozess — Angebot bis Zahlung — plus "
        "ausgewählte native Entitäten."
    ),
    description_en=(
        "The hand-curated core for your existing Xentral ERP: refined business "
        "objects for the main process — offer to payment — plus selected "
        "native entities."
    ),
)
