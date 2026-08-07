"""Schreibschutz is readable, filterable and switchable on every document.

v3 ships `setWriteProtection` / `removeWriteProtection` on all eight business
document types and exposes `writeProtection` as a boolean on every document
resource (`BusinessDocumentResource`: `writeProtection => isWriteProtected()`),
filterable via `QueryFilter::boolean('writeProtection', 'schreibschutz')`. The
core exposed none of it, so a clerk could neither see that a document was locked
nor unlock it — and the playbook recorded that as "not possible", which was true
of the core and false of the product.

Seven of the eight are core entities (there is no ProformaInvoice here).

Verified live on mvp, effect-based rather than on the 200: set it, read
`writeProtection: true`, PATCH a normal field → **409 write-protected**, lift it,
read false again.

The bypass is the part worth pinning. Upstream's `writeProtectionBypassFields()`
returns `['internebemerkung', 'status']`, so a protected document still accepts a
note write — measured, and it briefly fooled this probe. Anything that infers
"not protected" from a successful write is wrong; the field is the only answer.
"""

from __future__ import annotations

import pytest
from xentral_entity_cores.agentos_neo_xentral.manifest import CORE

# Every core entity whose v3 resource has the two actions.
PROTECTED_DOCUMENTS = {
    "Quote",
    "SalesOrder",
    "SalesInvoice",
    "DeliveryNote",
    "CreditNote",
    "Return",
    "PurchaseOrder",
}


def _adapters() -> dict:
    return {a.manifest.key: a for a in CORE.adapters}


@pytest.mark.parametrize("key", sorted(PROTECTED_DOCUMENTS))
def test_the_field_is_readable_and_filterable(key):
    meta = _adapters()[key].metadata(None)
    spec = meta["rootNode"]["properties"].get("writeProtection")
    assert spec is not None, f"{key} does not expose writeProtection"
    assert spec.get("access") == "readOnly", "it flips via the actions, not via update"
    assert spec.get("filterable"), "upstream filters on it — 'all locked invoices' is a query"


@pytest.mark.parametrize("key", sorted(PROTECTED_DOCUMENTS))
def test_both_actions_are_offered_and_executable(key):
    meta = _adapters()[key].metadata(None)
    actions = {a["key"]: a for a in meta.get("actions") or []}
    for op in ("setWriteProtection", "removeWriteProtection"):
        assert op in actions, f"{key}.{op} missing"
        assert not actions[op].get("wish"), f"{key}.{op} must not be declared as a wish"


@pytest.mark.parametrize("key", sorted(PROTECTED_DOCUMENTS))
def test_the_actions_route_to_the_v3_endpoints(key):
    """The catalogue text lives in the base; the routes must exist per adapter, or
    the action is advertised and then 404s."""
    adapter = _adapters()[key]
    for op in ("setWriteProtection", "removeWriteProtection"):
        assert op in adapter.action_map, f"{key} has no route for {op}"
        assert adapter.action_map[op] == ("PATCH", op)


def test_the_description_warns_about_the_bypass():
    """A successful note write is not evidence the document is unprotected —
    upstream lets `internebemerkung` and `status` through. An agent that does not
    know this will report a locked document as editable."""
    meta = _adapters()["SalesOrder"].metadata(None)
    actions = {a["key"]: a for a in meta.get("actions") or []}
    description = actions["setWriteProtection"].get("description") or ""
    assert "409" in description
    assert "status" in description and "note" in description


def test_entities_without_the_upstream_endpoints_do_not_offer_the_actions():
    """PurchaseInvoice is a BF entity with no write-protection endpoint, and the
    master-data entities have none either — declaring the actions there would
    promise a 404."""
    for key, adapter in _adapters().items():
        if key in PROTECTED_DOCUMENTS:
            continue
        keys = {a["key"] for a in adapter.metadata(None).get("actions") or []}
        assert not (keys & {"setWriteProtection", "removeWriteProtection"}), key
