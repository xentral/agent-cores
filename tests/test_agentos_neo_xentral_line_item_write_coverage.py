"""Every line-item field the schema calls writable is actually sent upstream.

CreditNote declared `items.description` creatable and never emitted it: the write
answered 201 and Xentral filled the product's description instead, so a caller who
supplied their own got someone else's text back with no error. The verify run saw
"sent on create but did not persist" and blamed upstream — the value never left
the core.

Xentral was fine throughout: `CreateProductLineItemData` declares the field,
`CreateLineItemAction` assigns it for every document type, and `description` maps
to the shared `beschreibung` column on `gutschrift_position` like everywhere else.
The gap was one missing line in one adapter.

The per-field test below pins that line. The coverage test is the one that
matters: it derives the expectation from the schema, so the next adapter that
promises a field and forgets to map it fails here instead of in production.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.credit_note import CreditNoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.quote import QuoteAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_invoice import SalesInvoiceAdapter
from xentral_entity_cores.agentos_neo_xentral.emulated.sales_order import SalesOrderAdapter

# adapter → the module whose _item_to_v3 does the mapping
_DOCS = [
    (QuoteAdapter, "quote"),
    (SalesOrderAdapter, "sales_order"),
    (SalesInvoiceAdapter, "sales_invoice"),
    (CreditNoteAdapter, "credit_note"),
]

# model key → the v3 wire key _item_to_v3 renames it to
_RENAME = {"unitPrice": "price", "discountPercent": "discount"}

_EMULATED = pathlib.Path(__file__).parent.parent / "cores/agentos_neo_xentral/emulated"


def _emitted_keys(module: str) -> set[str]:
    """The v3 keys `_item_to_v3` can put on the wire, read off the source. Static
    rather than executed so a field guarded by `if value is not None` still counts
    — the question is whether the mapping knows the field at all."""
    src = (_EMULATED / f"{module}.py").read_text(encoding="utf-8")
    body = src[src.index("def _item_to_v3") :]
    body = body[: body.index("return out")]
    return set(re.findall(r'out\["([a-zA-Z]+)"\]', body))


def _writable_item_fields(adapter_cls: type) -> set[str]:
    props: dict[str, Any] = (
        (adapter_cls().fields().get("items") or {}).get("node", {}).get("properties", {})
    )
    return {
        name
        for name, spec in props.items()
        if isinstance(spec, dict) and (spec.get("creatable") or spec.get("updatable"))
    }


@pytest.mark.parametrize(("adapter_cls", "module"), _DOCS, ids=[m for _, m in _DOCS])
def test_every_writable_item_field_is_actually_sent(adapter_cls: type, module: str) -> None:
    """A field the schema advertises as writable must reach the wire. Anything
    else is a silent drop: 201 back, value gone, nothing for the caller to see."""
    emitted = _emitted_keys(module)
    missing = sorted(
        name
        for name in _writable_item_fields(adapter_cls)
        if _RENAME.get(name, name) not in emitted
    )
    assert not missing, f"{adapter_cls.__name__} declares {missing} writable but never sends them"


def test_credit_note_sends_the_line_description() -> None:
    """The regression itself: it was the only gap of its kind across the four."""
    body, rejected = CreditNoteAdapter().map_write(
        {
            "items": [
                {
                    "product": {"id": "prd_61988"},
                    "quantity": {"value": 1},
                    "description": "MEINE-EIGENE-BESCHREIBUNG",
                }
            ]
        },
        creating=True,
    )
    assert rejected == set()
    assert body["lineItems"][0]["description"] == "MEINE-EIGENE-BESCHREIBUNG"


def test_all_four_agree_on_the_line_item_write_surface() -> None:
    """They map the same model onto the same v3 shape, so a key present in three
    and absent in the fourth is a mistake rather than a design choice — that is
    exactly how the CreditNote gap looked."""
    per_doc = {module: _emitted_keys(module) for _, module in _DOCS}
    everywhere = set.intersection(*per_doc.values())
    for module, keys in per_doc.items():
        assert keys == everywhere, f"{module} diverges: {sorted(keys ^ everywhere)}"
