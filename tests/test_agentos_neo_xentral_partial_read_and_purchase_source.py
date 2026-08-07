"""Two values a caller could not see, both found by costing a bill of materials.

**A failed sub-request looked exactly like an empty section.** A single product
read fans out to `/stocks`, `/parts`, `/salesPrices` and `/properties`, and a
sub-request that fails is deliberately not fatal — the record still comes back,
with `extra.unavailableSections` naming what could not be reached. But a
workflow's `business-entity` node hands its box the RECORD, not the envelope
(ADR-0002), so that sentence never arrived. A BOM cost roll-up whose `/parts`
call failed therefore saw `bom.items == []`, concluded "purchased article", and
wrote a wrong purchase price — no error, no empty result, nothing to notice.

**`prices.purchase.source` was declared read-only and was neither.** It was not
emitted on read, so it could not be read; and it WAS honoured by `map_write`,
where `"calculated"` is the only thing that sets Xentral's
`hasCalculatedPurchasePrice`. So the schema told every reader the flag could not
be set, while a read-edit-write round trip silently downgraded a calculated
purchase price to a manually maintained one — same number, different meaning.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter


def _adapter() -> ProductAdapter:
    return ProductAdapter.__new__(ProductAdapter)


def _cpp(flag: bool, amount: str = "12.50") -> dict:
    return {
        "calculatedPurchasePrice": {
            "hasCalculatedPurchasePrice": flag,
            "price": {"amount": amount, "currency": "EUR"},
        }
    }


# ---- a partial read says so ON THE RECORD --------------------------------


def _hydrate(failing: str | None) -> dict:
    """Run the detail hydration with one sub-resource refusing to answer."""
    adapter = _adapter()
    payload = {"data": {"id": "prd_1", "bom": {"items": []}, "prices": {}, "logistics": {}}}
    response = httpx.Response(
        200,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    async def _fetch_sub(suffix, up_id, *args, **kwargs):  # noqa: ANN001, ANN202
        return None if failing and failing in suffix else {"data": []}

    adapter._fetch_sub = _fetch_sub  # noqa: SLF001
    out = asyncio.run(adapter._hydrate_detail(response, "https://x.invalid", "t", None, None))  # noqa: SLF001
    return json.loads(out.content)


def test_an_unreachable_section_is_named_on_the_record():
    """The record is all a workflow ever sees, so the fact has to live there."""
    body = _hydrate("parts")
    assert body["data"]["_unavailableSections"] == ["bom"]


def test_the_envelope_still_says_it_too():
    """Kept for direct API consumers — this adds a place, it does not move one."""
    assert _hydrate("parts")["extra"] == {"unavailableSections": ["bom"]}


def test_an_empty_section_is_not_an_unreachable_one():
    """The whole point: `bom.items == []` from a healthy read must stay clean,
    or the signal means nothing."""
    body = _hydrate(None)
    assert "_unavailableSections" not in body["data"]
    assert "extra" not in body
    assert body["data"]["bom"] == {"items": []}


def test_the_marker_names_only_what_actually_failed():
    body = _hydrate("salesPrices")
    assert body["data"]["_unavailableSections"] == ["prices.sale"]


# ---- prices.purchase.source round-trips ----------------------------------


def test_a_calculated_price_reads_as_calculated():
    assert _adapter().map_read({"id": 1, **_cpp(True)})["prices"]["purchase"]["source"] == (
        "calculated"
    )


def test_a_manual_price_reads_as_manual():
    assert _adapter().map_read({"id": 1, **_cpp(False)})["prices"]["purchase"]["source"] == "manual"


def test_no_price_stays_none_rather_than_a_bare_source():
    """`source` must not conjure a price object where there is no price."""
    assert _adapter().map_read({"id": 1})["prices"]["purchase"] is None


def test_the_flag_survives_a_read_edit_write_round_trip():
    """The failure this closes: send back exactly what you were given, and the
    calculated price came out the other side as a manual one."""
    adapter = _adapter()
    for flag in (True, False):
        record = adapter.map_read({"id": 1, **_cpp(flag)})
        body, rejected = adapter.map_write(
            {"prices": {"purchase": record["prices"]["purchase"]}}, creating=False
        )
        assert not rejected
        assert body["calculatedPurchasePrice"]["hasCalculatedPurchasePrice"] is flag
        assert body["calculatedPurchasePrice"]["price"]["amount"] == "12.50"


def test_source_is_writable_in_the_schema_because_the_write_honours_it():
    """It was marked read-only while `map_write` read it — a schema reader would
    conclude the flag cannot be set, and be wrong."""
    purchase = _adapter().fields()["prices"]["properties"]["purchase"]
    source = purchase["properties"]["source"]
    assert source.get("access") != "readOnly"
    assert source.get("creatable") and source.get("updatable")
