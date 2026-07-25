"""weclapp native roster — generated from an OpenAPI spec at build time.

``build_adapters`` loads the spec (a real tenant export ``openapi.json`` when
present, otherwise the committed ``openapi.sample.json``), runs the generator, and
wraps each Entity in the shared engine adapter. No network — the spec is a
build-time artifact. Credentials are resolved under this core's OWN
``weclapp_core`` connector — every core keeps its own, separate connection.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from entity_registry.core_sdk import register_core_fields

from xentral_entity_cores.agentos_neo_weclapp.emulated.base import (
    WECLAPP_FIELDS,
    WeclappAdapterBase,
)

# This core's own connector id — its connection is created/edited/tested under
# this id, fully separate from the agentos_neo_weclapp core's connection.
CONNECTOR_ID = "weclapp_core"
register_core_fields(CONNECTOR_ID, WECLAPP_FIELDS)

from .generator import build_entities_from_openapi

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent
# A real tenant-exported spec wins over the demonstrative sample.
_SPEC_CANDIDATES = ("openapi.json", "openapi.sample.json")


def _load_spec() -> dict:
    for name in _SPEC_CANDIDATES:
        path = _DIR / name
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError) as exc:  # a broken spec must not break the catalogue
                logger.warning("weclapp_core: could not read %s: %s", name, exc)
    return {}


def build_adapters() -> tuple[WeclappAdapterBase, ...]:
    """The native weclapp roster, generated from the spec. Never raises — a missing
    or broken spec yields an empty roster rather than a broken catalogue."""
    return tuple(
        WeclappAdapterBase(e, connector_id=CONNECTOR_ID)
        for e in build_entities_from_openapi(_load_spec())
    )
