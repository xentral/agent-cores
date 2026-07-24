from __future__ import annotations

import json
from pathlib import Path

from .base import PostgresEntityAdapter, snake

_MODEL = Path(__file__).resolve().parent.parent / "model.json"


def build_adapters() -> tuple[PostgresEntityAdapter, ...]:
    """One adapter per entity of the generated Neo model snapshot. Static —
    the roster is the model, not a live backend, so this resolves at import."""
    model = json.loads(_MODEL.read_text(encoding="utf-8"))
    tables = ["neo_" + snake(e["key"]) for e in model["entities"]]
    return tuple(PostgresEntityAdapter(e, model["modelVersion"], tables) for e in model["entities"])


__all__ = ["build_adapters"]
