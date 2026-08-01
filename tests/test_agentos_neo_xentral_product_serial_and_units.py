"""Serial-number tracking survives a round-trip; logistics units are read-only.

Two findings from the same corner of the Product schema.

**The serial-number mode was corrupted on write.** The two upstream generations
use different vocabularies for the same setting and they do NOT line up in order.
Both write the same legacy column, which is what makes the pairing knowable
(monorepo: `ProductSerialNumberTracking::MAPPING`, `ProductEntity::SERIAL_NUMBERS_MODE`):

    legacy               v3 read           v2 write
    keine                none              disabled
    eigene               stockGenerated    user
    vomprodukteinlagern  stockOriginal     productAndWarehouse
    vomprodukt           trackOriginal     product

The core read v3 spellings and wrote v2 ones with `sn if sn in <v2 modes> else
"disabled"`. Since no v3 spelling is a v2 mode, writing back what you just read
**turned tracking off**. Every product on the test tenant sits at `none`, so the
verify run could never have caught it — the failing cell said only "not persisted".

**The logistics units are not writable at all.** v3 returns weight and dimensions
as bare numbers, documented as "Weight in kg." / "Length in cm." — there is no
unit field upstream. The core fills kg/cm from that contract, so declaring `unit`
creatable promised a write with nothing behind it, which is exactly what
"upstream accepted the write but the value did not persist" meant.
"""

from __future__ import annotations

import pytest

from xentral_entity_cores.agentos_neo_xentral.emulated.product import ProductAdapter


def _write(value: str) -> tuple[dict, set[str]]:
    return ProductAdapter().map_write({"tracking": {"serialNumbers": value}}, creating=False)


@pytest.mark.parametrize(
    ("model_value", "v2_value"),
    [
        ("none", "disabled"),
        ("stockGenerated", "user"),
        # the crossing — a positional mapping would swap these two
        ("stockOriginal", "productAndWarehouse"),
        ("trackOriginal", "product"),
    ],
)
def test_each_v3_mode_maps_to_its_own_v2_mode(model_value: str, v2_value: str) -> None:
    body, rejected = _write(model_value)
    assert rejected == set()
    assert body["serialNumbersMode"] == v2_value


@pytest.mark.parametrize("model_value", ["stockGenerated", "stockOriginal", "trackOriginal"])
def test_writing_back_a_read_value_no_longer_disables_tracking(model_value: str) -> None:
    """The regression: all three used to fall through to "disabled"."""
    body, _ = _write(model_value)
    assert body["serialNumbersMode"] != "disabled"


@pytest.mark.parametrize("v2_value", ["disabled", "user", "product", "productAndWarehouse"])
def test_the_v2_spellings_are_accepted_too(v2_value: str) -> None:
    """A caller already speaking the v2 dialect should not be punished for it."""
    body, rejected = _write(v2_value)
    assert rejected == set()
    assert body["serialNumbersMode"] == v2_value


@pytest.mark.parametrize("bad", ["off", "keine", "SERIAL", ""])
def test_an_unknown_mode_is_refused_not_silently_disabled(bad: str) -> None:
    """Losing serial-number tracking must never be the quiet default."""
    body, rejected = _write(bad)
    assert "tracking.serialNumbers" in rejected
    assert "serialNumbersMode" not in body


def test_the_options_are_declared_and_match_what_the_write_accepts() -> None:
    props = ProductAdapter().fields()["tracking"]["properties"]["serialNumbers"]
    declared = {o["value"] for o in props["options"]}
    assert declared == {"none", "stockGenerated", "stockOriginal", "trackOriginal"}
    for value in declared:
        assert _write(value)[1] == set(), value


@pytest.mark.parametrize("block", ["weight", "netWeight", "dimensions"])
def test_logistics_units_are_read_only(block: str) -> None:
    """No unit field exists upstream — the value comes from the API's own contract
    (kg / cm), so the schema must not advertise a write."""
    spec = ProductAdapter().fields()["logistics"]["properties"][block]["properties"]["unit"]
    assert spec.get("access") == "readOnly"
    assert not spec.get("creatable") and not spec.get("updatable")


def test_the_measured_value_is_still_writable() -> None:
    """Only the unit is fixed; the number itself stays writable."""
    spec = ProductAdapter().fields()["logistics"]["properties"]["weight"]["properties"]["value"]
    assert spec.get("updatable") is True
