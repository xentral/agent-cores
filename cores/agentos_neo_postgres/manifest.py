"""AgentOS Neo (standalone) — the Neo model on a tenant-owned Postgres.

Same outward model as ``agentos_neo_xentral`` (the schemas ship as a generated
snapshot, ``model.json`` — regenerate with
``scripts/generate_neo_postgres_model.py``), but records live in the tenant's
own Postgres database: the tenant connects host/port/database/user/password
like any other integration, the core bootstraps its tables on first use, and
every contract surface (tables, workflows, dashboards, MCP) works against it
out of the box. See ``emulated/base.py`` for the storage contract.
"""

from __future__ import annotations

from entity_registry.core_sdk import CoreManifest, EmulatedOnly

from .emulated import build_adapters

CORE = CoreManifest(
    id="agentos_neo_postgres",
    label_de="AgentOS Neo (eigene Datenbank)",
    label_en="AgentOS Neo (standalone database)",
    order=9,
    native_policy=EmulatedOnly(),
    adapters=build_adapters(),
    labs=True,
    description_de=(
        "Das AgentOS-Neo-Datenmodell auf deiner eigenen Postgres-Datenbank: "
        "Zugangsdaten verbinden, die Tabellen werden automatisch angelegt — "
        "kein ERP dahinter nötig."
    ),
    description_en=(
        "The AgentOS Neo data model on your own Postgres database: connect "
        "your credentials and the tables are created automatically — no ERP "
        "behind it required."
    ),
)
