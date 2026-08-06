"""Business-mandatory fields are declared, and declared in the readable dialect.

Before this, the core marked exactly one field mandatory (Return.items.reason)
and it used a spelling the workspace form does not parse — so from the outside
the whole model read as "everything is optional", and the only feedback on a
create missing its customer was an upstream 400.

Two things are locked in here:

* the marker uses ``rules: ["required"]`` — native Xentral's dialect, the one
  the form and the MCP field view both read;
* every entity that can be created declares at least one, so a new entity
  cannot quietly land with no mandatory field at all.
"""

from __future__ import annotations

import pytest
from xentral_entity_cores.agentos_neo_xentral.manifest import CORE


def _adapters():
    return [a for a in CORE.adapters if "create" in (a.manifest.operations or ())]


def _walk(props, prefix=""):
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        yield path, spec
        node = spec.get("node")
        nested = node.get("properties") if isinstance(node, dict) else spec.get("properties")
        if isinstance(nested, dict):
            yield from _walk(nested, f"{path}.")


def _required_paths(adapter) -> set[str]:
    return {
        path for path, spec in _walk(adapter.fields()) if "required" in (spec.get("rules") or [])
    }


# A record without these is not the thing it claims to be.
EXPECTED = {
    "SalesOrder": {"customer", "items.quantity"},
    "Quote": {"customer", "items.quantity"},
    "SalesInvoice": {"customer", "items.quantity"},
    "CreditNote": {"customer", "items.quantity"},
    "DeliveryNote": {"customer", "items.quantity"},
    "Return": {"customer", "items.quantity", "items.reason"},
    "PurchaseOrder": {"supplier", "items.quantity"},
    "PurchaseInvoice": {"supplier"},
    "Customer": {"name", "contacts.name"},
    "Supplier": {"name", "contacts.name"},
    "Product": {"name"},
    "PurchasePrice": {"product", "supplier"},
    "PriceList": {"product"},
    "StorageLocation": {"name", "warehouse"},
    "StockMovement": {"type", "product"},
    "Warehouse": {"name"},
    "Task": {"title"},
}


@pytest.mark.parametrize("key,expected", sorted(EXPECTED.items()))
def test_entity_declares_its_mandatory_fields(key, expected):
    [adapter] = [a for a in _adapters() if a.manifest.key == key]
    assert expected <= _required_paths(adapter)


def test_every_creatable_entity_declares_at_least_one_mandatory_field():
    bare = sorted(a.manifest.key for a in _adapters() if not _required_paths(a))
    assert not bare, f"creatable entities with no mandatory field: {bare}"


def test_no_entity_uses_the_bare_required_flag():
    """The spelling the workspace form cannot see."""
    offenders = [
        f"{a.manifest.key}.{path}"
        for a in CORE.adapters
        for path, spec in _walk(a.fields())
        if spec.get("required") is True and "required" not in (spec.get("rules") or [])
    ]
    assert not offenders, f"bare `required: True` (invisible to the form): {offenders}"
