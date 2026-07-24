"""Phoenix core — the entity roster, resolved live.

Nothing about Phoenix is modeled by hand. ``GET /api/metadata`` serves the
entity index (key, label, domain, operations) and ``GET /api/metadata/{key}``
the per-entity contract; both are fetched live per tenant from that tenant's own
Phoenix connection. A new entity (a class with ``#[BusinessEntity]`` dropped into
the Phoenix backend) therefore appears automatically — there is no static list to
keep in sync here.

Inspect the raw roster with:  curl -sk https://phoenix-backend.eu.xentral.dev/api/metadata
"""

from __future__ import annotations

from entity_registry.core_sdk import EmulationManifest

from .base import PhoenixAdapterBase, fetch_phoenix_roster


class PhoenixEntityAdapter(PhoenixAdapterBase):
    def __init__(
        self,
        key: str,
        label_en: str,
        domain: str,
        operations: tuple[str, ...] = (),
    ) -> None:
        ops = tuple(operations) or ("list", "read", "create", "update", "delete")
        # Phoenix reports CRUD verbs; our consumers also expect an explicit
        # ``list``. Mirror the same derivation the metadata path applies.
        if "read" in ops and "list" not in ops:
            ops = ("list", *ops)
        self.manifest = EmulationManifest(
            key=key,
            label_en=label_en,
            category=domain.lower(),
            rollout_batch="phoenix",
            adapter=f"phoenix.{key}",
            source_apis=("phoenix",),
            operations=ops,
        )


def build_adapters() -> tuple[PhoenixEntityAdapter, ...]:
    """The Phoenix adapters for the current tenant, built from the live roster.

    Empty when the tenant has no Phoenix connection configured — the core then
    exposes no entities until one is set up. Wired as the core's
    ``adapters_factory`` so it is re-resolved per request (behind a short TTL
    cache in :func:`fetch_phoenix_roster`), never at import time."""
    return tuple(PhoenixEntityAdapter(*entry) for entry in fetch_phoenix_roster())
