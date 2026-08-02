"""Full-facet verification for the agentos_neo_xentral facade core — the GREEN/RED side of
the living backlog (docs/03-mapping-layer.md §5).

Every verdict says how strongly the claim was shown, not merely that a request
succeeded — the vocabulary and the reasoning live in ``verdicts.py``. A probe that
can only watch an HTTP status records `accepted`, never `pass`.

Per entity, THROUGH the facade contract (adapter.request — exactly what consumers
hit):
  read    — list 10 + 100 records (and the sampled record's single read where the
            entity has detail-only sections); a path is `pass` where some record
            actually CARRIED a value, else `unobserved`. Collections are walked, so
            line-item fields are reachable. `unobserved` is not a failure: mvp is a
            narrow instance and a field nobody fills there is unproven either way.
  filter  — per filterable field: probe filter[0][key]=<model path> with a value
            from a real sampled record (references probe by stripped id), then
            require that record back. Several v3 list endpoints answer 200 while
            ignoring the filter, which a status check grades green — those are
            `accepted`, with the note saying so.
  sort    — per sortable field: request BOTH directions and compare. Ordering that
            survives a reversal is `pass`; an accepted key that changes nothing is
            `fail`; too few distinct values, or an order we cannot read (a
            reference sorted by the referent's name) is `accepted`.
  search  — per searchable field, through the contract the facade actually parses
            (filter[i][key]=search → per-field `contains` fan-out), requiring the
            sampled record back. A field the schema flags `searchable` but
            ``search_fields()`` never reaches is `fail`: a consolidated search
            cannot match on it.
  update  — SAFE roundtrip on every updatable scalar leaf (string/date/number/
            boolean + toggle-selects): set marker → read back → verify → restore
            (net-zero). Numeric/boolean leaves are probed only where the sample
            carries a value (a null cannot be restored net-zero — create covers those).
  create  — documents (partner ref + note) and the master price/product entities
            (Product, PriceList, PurchasePrice): POST a rich record, verify each
            field persisted, then DELETE it (net-zero). Product has NO delete →
            opt-in via VERIFY_CREATE_NONZERO, leaving one labelled record behind.
            This runner IS the destructive suite for this core.

actions + process steps (opt-in, VERIFY_ACTIONS=1 — these EXECUTE real upstream
  operations on a sampled record, so they are NOT net-zero): each is invoked
  through the facade with an empty command.
    pass       — the effect was seen. Only the net-zero probes reach it: the
                 addTag/removeTag roundtrip and downloadPdf (which renders a
                 document and changes nothing, so the body can be checked for
                 `%PDF-`).
    executed   — 2xx, effect not read back.
    reachable  — the route refused our empty command (400/422) or the record's
                 state (409). The note says whether the refusal came from upstream
                 or from the core's own validator, which proves far less.
    fail       — 404/405/501/5xx, or a 200 that did not render a PDF.
    409-wish   — stays blue, unmeasured.
  For `send` (mails a real customer) and `cancel` (mutates a document) `pass` is
  unreachable on a live tenant. That is their final answer, not a to-do.

  A 401/403 anywhere ABORTS the run with exit 2 and writes nothing. It is a broken
  run, not a property of a capability: it used to grade `pass` ("reachable — 401"),
  so an expired token painted whole suites green, and grading it `fail` would be as
  wrong in the other direction.

Failures carry ``<facet>Note`` with the upstream error so the grid explains every
red cell. Untestable probes (no sample value) stay grey with a note. Re-running
after an upstream change flips cells automatically — nobody maintains a list.

Env:
  DUMP_INSTANCE=<uuid>   target tenant (defaults to the mvp test instance)
  XENTRAL_API_KEY=id|hash  static Sanctum token, preferred for entity/action routes
  VERIFY_ACTIONS=1       also probe actions + process steps (executes upstream ops)
  VERIFY_CREATE_NONZERO=1  also create-probe entities without a delete endpoint
                         (Product) — NOT net-zero, leaves one labelled record
  VERIFY_ONLY=A,B        re-probe only these entities and MERGE into verified.json
                         (never wipes the entities it didn't touch)

Run: this module uses a relative import (``from ..manifest import CORE``), so it
only runs inside the synthetic ``xentral_entity_cores`` package the backend
registers (see conftest.py) — the old ``entity_registry.cores.…`` path is gone
since the cores moved to this repo. Env is read at import time, so set it first.
``XENTRAL_BASE_URL`` + ``XENTRAL_API_KEY`` together take the fast path in _auth()
and skip Auth0 entirely::

    XENTRAL_BASE_URL=https://mvp.xentral.biz XENTRAL_API_KEY=<id|hash> \\
    DUMP_INSTANCE=<uuid> VERIFY_ACTIONS=1 \\
      uv run --project <agent-os>/backend python -c "
    import sys, types, asyncio, pathlib
    p = types.ModuleType('xentral_entity_cores')
    p.__path__ = [str(pathlib.Path('cores').resolve())]
    sys.modules['xentral_entity_cores'] = p
    sys.path.insert(0, '<agent-os>/backend')
    from xentral_entity_cores.agentos_neo_xentral.checks.verify import _main
    asyncio.run(_main())"

Careful: this module's own load_dotenv points four levels up from checks/ — also a
leftover from the in-backend days — so it does NOT read the backend's .env. Pass
the credentials explicitly.

Render the result as a workbook: ``scripts/export_verified_xlsx.py``.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import os
import re
from datetime import datetime, UTC
import secrets
from typing import Any

import httpx

# Load backend/.env so a bare `uv run python -m …` picks up XENTRAL_API_KEY (the
# Sanctum token) and friends without the caller pre-exporting them. Does not
# override vars already set on the command line (load_dotenv default), so
# DUMP_INSTANCE=… / XENTRAL_BASE_URL=… overrides still win.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".env"))
except Exception:  # noqa: BLE001, S110 — env file is optional; config may come from the shell
    pass

from ..emulated.base import STATUS_FALLBACKS
from ..manifest import CORE
from ..verdicts import PROVEN

_INSTANCE = os.environ.get("DUMP_INSTANCE", "a2ad4180-0d41-4360-8044-9dca0c35608a")
# Actions + process steps EXECUTE real upstream operations on the sampled record
# (confirm/cancel/createDeliveryNote …) — they are NOT net-zero like the field
# suite. So they are opt-in: set VERIFY_ACTIONS=1 to probe them.
_PROBE_ACTIONS = os.environ.get("VERIFY_ACTIONS") == "1"
# …and even then it is all-or-nothing per entity, which is a heavy price for
# re-checking ONE action: probing `send` again mails another customer of the
# sampled record. VERIFY_ACTIONS_ONLY=downloadPdf,release narrows the run to the
# named keys and leaves every other verdict untouched (the merge in _main carries
# the previous ones through).
_ACTIONS_ONLY = {
    k.strip() for k in os.environ.get("VERIFY_ACTIONS_ONLY", "").split(",") if k.strip()
}
# A create probe on an entity WITHOUT a delete endpoint (Product) cannot be net-zero
# — it leaves a clearly-labelled record behind. Opt-in so a normal run stays clean;
# entities that DO have delete (PriceList, PurchasePrice) always run net-zero.
_PROBE_CREATE_NONZERO = os.environ.get("VERIFY_CREATE_NONZERO") == "1"
# Comma-separated entity keys to (re)probe; when set, the run MERGES into the
# existing verified.json instead of overwriting it — fast, low-side-effect
# re-checks of just the entities you care about (e.g. VERIFY_ONLY=Product).
_ONLY = {k.strip() for k in os.environ.get("VERIFY_ONLY", "").split(",") if k.strip()}
# No leading whitespace — upstream trims strings, which would false-fail the
# persistence check when the original value is empty.
_MARK = "·vt"
# Format-validated fields need a VALID toggle value, not a fuzz marker — a
# rejection must mean "not writable", never "bad test value". Keyed by leaf name.
_VALID_TOGGLE = {
    "currency": ("EUR", "USD"),
    "email": ("verify-a@example.test", "verify-b@example.test"),
    "language": ("EN", "DE"),
    "country": ("DE", "AT"),
    "state": ("BE", "BW"),
    "taxation": ("domestic", "eu"),
}
# Valid per-leaf values for address blocks sent on CREATE.
_ADDR_CREATE = {
    "name": "VT Verify GmbH",
    "street": "Verifystraße 1",
    "zip": "86150",
    "city": "Augsburg",
    "country": "DE",
    "email": "verify@example.test",
    "phone": "+49 821 000000",
    "vatId": "DE123456789",
    "state": "BY",
}
# Master-data create payload (Customer/Supplier) — created live, then DELETEd.
_MASTER_CREATE = {
    "name": "VT Verify GmbH",
    "email": "verify@example.test",
    "phone": "+49 821 000000",
    "website": "https://example.test",
    "vatId": "DE123456789",
    "language": "EN",
}
# Search fixture values for fields no sampled record carries.
# Kept for the rebuilt search probe: values for fields no sampled record carries, so
# a search can still be exercised. Unused while the search probe is withdrawn (it
# measured the wrong contract — see the `searchable` branch in _verify_entity).
_SEARCH_FIXTURES = {"vatId": "DE123456789", "ean": "4001234567890"}
# Documents that take a create probe (minimal partner + note, then DELETE).
_CREATE_PARTNER = {
    "SalesOrder": "customer",
    "SalesInvoice": "customer",
    "DeliveryNote": "customer",
    "CreditNote": "customer",
    "Quote": "customer",
    "Return": "customer",
    "PurchaseOrder": "supplier",
}


def _field_paths(props: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}.{name}" if prefix else name
        out.append(path)
        sub = spec.get("properties") or (spec.get("node") or {}).get("properties")
        if isinstance(sub, dict):
            out += _field_paths(sub, path)
    return out


def _walk(props: dict[str, Any], prefix: str = ""):
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}.{name}" if prefix else name
        yield path, spec
        sub = spec.get("properties") or (spec.get("node") or {}).get("properties")
        if isinstance(sub, dict):
            yield from _walk(sub, path)


def _value_at(rec: Any, path: str) -> Any:
    node = rec
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _is_filled(value: Any) -> bool:
    """Whether a record actually carried something at this path.

    `False` and `0` count — they are values a merchant set. Only absence counts as
    nothing, which is why this is not a truth test.
    """
    return value is not None and value not in ("", [], {})


def _values_at(rec: Any, path: str) -> list[Any]:
    """Every value ``path`` reaches in one record, fanning out through collections.

    ``_value_at`` stops at the first list, so `items.description` always came back
    ``None`` — which is why no line-item field could ever be observed. Here a list
    segment yields one value per element instead.
    """
    nodes: list[Any] = [rec]
    for part in path.split("."):
        step: list[Any] = []
        for node in nodes:
            if isinstance(node, list):
                step += [item.get(part) for item in node if isinstance(item, dict)]
            elif isinstance(node, dict):
                step.append(node.get(part))
        nodes = step
    return nodes


def _observed(records: list[Any], path: str) -> bool:
    """Whether ANY of the records carried a value at ``path``."""
    return any(_is_filled(v) for rec in records for v in _values_at(rec, path))


def _sort_key(value: Any) -> Any:
    """A comparable stand-in for one row's value at the sorted path.

    References arrive as ``{"id": …, "name": …}``; upstream may order them by the
    referent's name while the row carries only the id, so those are compared on
    whatever text is there and fall out as unassertable when it is not.
    """
    if isinstance(value, dict):
        value = value.get("name") or value.get("id")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value).casefold() if value is not None else None


def _sort_effect(
    path: str, desc_payload: Any, asc_payload: Any, asc_status: int
) -> tuple[str, str | None]:
    """Did the sort key actually order the page, or was it merely accepted?

    A status check alone cannot tell: an upstream that does not know the key answers
    200 with its default order. So both directions are requested and compared —
    ordering that survives a reversal is the claim `sort` makes.
    """
    if asc_status != 200:
        return "accepted", f"descending accepted; ascending answered {asc_status}"
    desc = [_sort_key(v) for r in (desc_payload.get("data") or []) for v in [_value_at(r, path)]]
    asc = [_sort_key(v) for r in (asc_payload.get("data") or []) for v in [_value_at(r, path)]]
    usable = [v for v in desc if v is not None]
    if len(usable) < 2 or len({v for v in usable}) < 2:
        return "accepted", "fewer than two distinct values on the page — order not assertable"
    try:
        monotone_desc = all(
            a >= b for a, b in zip(desc, desc[1:], strict=False) if a is not None and b is not None
        )
        monotone_asc = all(
            a <= b for a, b in zip(asc, asc[1:], strict=False) if a is not None and b is not None
        )
    except TypeError:  # mixed types on the page — comparing them proves nothing
        return "accepted", "values on the page are not mutually comparable"
    if monotone_desc and monotone_asc:
        return PROVEN, None
    if desc == asc:
        return (
            "fail",
            "the sort key was accepted but both directions returned the same order — "
            "it appears to be ignored",
        )
    return (
        "accepted",
        "the order changed with direction but is not monotone in the value we can read",
    )


def _line_toggle(spec: dict[str, Any], name: str, orig: Any) -> tuple[Any, Any]:
    """A net-zero test value for ONE line field, plus what restores it.

    Returns ``(None, None)`` when the sampled line carries nothing to toggle from:
    a null price or quantity cannot be put back the way it was, and "restored to a
    guess" is worse than "not probed".
    """
    if isinstance(orig, dict) and "amount" in orig:  # money
        try:
            amount = float(orig.get("amount"))
        except (TypeError, ValueError):
            return None, None
        cur = orig.get("currency") or "EUR"
        return (
            {"amount": f"{amount + 1:.2f}", "currency": cur},
            {"amount": f"{amount:.2f}", "currency": cur},
        )
    if isinstance(orig, dict) and "value" in orig:  # quantity
        try:
            value = float(orig.get("value"))
        except (TypeError, ValueError):
            return None, None
        unit = orig.get("unit")
        return ({"value": value + 1, "unit": unit}, {"value": value, "unit": unit})
    if isinstance(orig, bool):
        return (not orig), orig
    if isinstance(orig, (int, float)):
        return orig + 1, orig
    if isinstance(orig, str):
        if (alt := _option_toggle(spec, orig)) is not None:
            return alt, orig
        if (num := _numeric_string_toggle(orig)) is not None:
            return num, orig
        if (clock := _time_string_toggle(orig)) is not None:
            return clock, orig
        clean = orig.replace(_MARK, "").strip()
        if not clean:
            return None, None  # an empty string restores to "" — upstream ignores it
        return (clean + _MARK if orig != clean + _MARK else clean), orig
    return None, None  # null: nothing to restore to


def _time_string_toggle(orig: Any) -> str | None:
    """A wall-clock string bumped by an hour, or None if it is not one.

    Without this the generic fallback appends the ``·vt`` marker, which upstream
    accepts with a 200 and silently normalises to ``00:00:00`` — the probe then
    reports a writable field as "accepted the write but the value did not
    persist". Measured on Task.dates.dueTime: `14:30:00` and `23:59:59` both
    round-trip, `vt-marker` becomes midnight.
    """
    if not isinstance(orig, str):
        return None
    m = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", orig)
    if not m:
        return None
    hour = (int(m.group(1)) + 1) % 24
    return f"{hour:02d}:{m.group(2)}:{m.group(3) or '00'}"


def _line_eq(got: Any, want: Any) -> bool:
    """Compare a line value the way the wire returns it (money as strings, a
    quantity as a number that may come back as 3 or 3.0)."""
    if isinstance(want, dict) and isinstance(got, dict):
        if "amount" in want:
            try:
                return float(got.get("amount")) == float(want["amount"])
            except (TypeError, ValueError):
                return False
        if "value" in want:
            try:
                return float(got.get("value")) == float(want["value"])
            except (TypeError, ValueError):
                return False
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return float(got) == float(want)
    return got == want


def _update_targets(props: dict[str, Any], prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every updatable scalar leaf (string/date/number/boolean) reachable WITHOUT
    crossing a collection — collections (items, tags) need per-item probes, not a
    PATCH of the whole list. Read-only embedded parents are not descended. Numeric
    and boolean leaves are probed only where the sample carries a value (a null
    number/flag cannot be restored net-zero — the create probe covers those)."""
    out: list[tuple[str, dict[str, Any]]] = []
    for name, spec in (props or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}.{name}" if prefix else name
        if spec.get("type") == "collection":
            continue
        sub = spec.get("properties")
        if isinstance(sub, dict) and spec.get("type") == "embedded":
            if spec.get("access") != "readOnly":
                out += _update_targets(sub, path)
            continue
        leaf = path.rsplit(".", 1)[-1]
        if spec.get("updatable") and (
            spec.get("type") in ("string", "date", "integer", "decimal", "number", "boolean")
            # A select is probeable once we can name a second valid value — either
            # from _VALID_TOGGLE or, better, from the field's own declared options.
            or (
                spec.get("type") == "select"
                and (leaf in _VALID_TOGGLE or len(spec.get("options") or []) >= 2)
            )
        ):
            out.append((path, spec))
    return out


def _nested_body(path: str, value: Any) -> dict[str, Any]:
    """``a.b.c`` + value → ``{"a": {"b": {"c": value}}}``."""
    body: Any = value
    for part in reversed(path.split(".")):
        body = {part: body}
    return body


def _got(data: dict[str, Any], path: str) -> Any:
    """Read-back value at ``path``; ``items.<sub>`` reads the FIRST line item."""
    if path.startswith("items."):
        items = data.get("items") or []
        first = items[0] if items and isinstance(items[0], dict) else {}
        return _value_at(first, path.split(".", 1)[1])
    return _value_at(data, path)


def _option_toggle(spec: dict[str, Any], orig: Any) -> str | None:
    """Another value from the field's declared ``options``, or None when it has
    none. A constrained field must be probed with a value it can actually hold —
    otherwise a refusal means "bad test value", never "not writable"."""
    options = spec.get("options")
    if not isinstance(options, list) or len(options) < 2:
        return None
    values: list[str] = []
    for opt in options:
        value = opt.get("value") if isinstance(opt, dict) else opt
        if isinstance(value, str) and value:
            values.append(value)
    if len(values) < 2:
        return None
    return next((v for v in values if v != orig), None)


def _numeric_string_toggle(orig: Any) -> str | None:
    """``"4.00"`` → ``"5.00"`` — a different number in the same shape, for values
    the schema types as string but the upstream parses as a number (money above
    all). None when the value is not numeric, so free text still gets the marker.

    Keeps the original's decimal places so a two-decimal money field stays
    two-decimal, and always steps UP — a field constrained to non-negative values
    must never be handed a negative probe.
    """
    if not isinstance(orig, str) or not orig.strip():
        return None
    try:
        value = float(orig)
    except (TypeError, ValueError):
        return None
    decimals = len(orig.partition(".")[2]) if "." in orig else 0
    return f"{value + 1:.{decimals}f}"


def _stamp() -> str:
    """UTC, seconds — one shape for every timestamp this file writes."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _cmp(kind: str, got: Any, want: Any) -> bool:
    """Persistence comparison per value kind."""
    if kind == "eq":
        return got == want
    if kind == "money":
        return isinstance(got, dict) and got.get("amount") == want
    if kind == "qty":
        try:
            return isinstance(got, dict) and float(got.get("value") or 0) == float(want)
        except (TypeError, ValueError):
            return False
    if kind == "ref":
        return isinstance(got, dict) and str(got.get("id") or "").endswith(str(want))
    if kind == "num":
        try:
            return float(got) == float(want)
        except (TypeError, ValueError):
            return False
    if kind == "tag":
        return isinstance(got, list) and want in got
    if kind == "present":
        return bool(got)
    return False


def _doc_create_payload(
    schema: dict[str, Any],
    writable: set[str],
    sample: dict[str, Any],
    product_id: str | None,
    tag_title: str | None,
) -> tuple[dict[str, Any], list[tuple[str, str, Any]]]:
    """Build the FULL create body a document adapter accepts, plus the per-path
    persistence expectations ``(path, compare-kind, wanted)``. Every creatable
    field the schema declares gets a valid value — the whole point is per-field
    create coverage, not a minimal record."""
    body: dict[str, Any] = {}
    expects: list[tuple[str, str, Any]] = []
    for name, spec in schema.items():
        if not isinstance(spec, dict) or name not in writable:
            continue
        t = spec.get("type")
        if t == "embedded" and name.endswith("Address"):
            subs = spec.get("properties") or {}
            addr = {
                k: _ADDR_CREATE[k]
                for k, s in subs.items()
                if isinstance(s, dict) and s.get("creatable") and k in _ADDR_CREATE
            }
            if addr:
                body[name] = addr
                expects += [(f"{name}.{k}", "eq", v) for k, v in addr.items()]
        elif name == "texts":
            body["texts"] = {"intro": "verify-suite intro", "outro": "verify-suite outro"}
            expects += [
                ("texts.intro", "eq", "verify-suite intro"),
                ("texts.outro", "eq", "verify-suite outro"),
            ]
        elif name == "dates":
            for cand, sub in (spec.get("properties") or {}).items():
                if isinstance(sub, dict) and sub.get("creatable") and sub.get("type") == "date":
                    body.setdefault("dates", {})[cand] = "2026-01-15"
                    expects.append((f"dates.{cand}", "eq", "2026-01-15"))
        elif name == "items" and product_id:
            subs = (spec.get("node") or {}).get("properties") or {}
            item: dict[str, Any] = {
                "product": {"id": product_id},
                "quantity": {"value": 2, "unit": "piece"},
            }
            expects += [("items.product", "ref", product_id), ("items.quantity", "qty", 2)]
            extras: list[tuple[str, Any, str, Any]] = [
                ("description", "verify item", "eq", "verify item"),
                ("unitPrice", {"amount": "9.90", "currency": "EUR"}, "money", "9.90"),
                ("discountPercent", 5, "num", 5),
                ("taxRate", "standard", "eq", "standard"),
                # The position's own cost price (API-780). It was declared
                # creatable on all four sales documents and never sent here, so its
                # create cell stayed blank on every one of them.
                ("purchasePrice", {"amount": "3.33", "currency": "EUR"}, "money", "3.33"),
                ("supplierProductNumber", "VT-SPN", "eq", "VT-SPN"),
                ("supplierProductName", "VT Supplier Name", "eq", "VT Supplier Name"),
            ]
            for sub, sent, kind, want in extras:
                s = subs.get(sub)
                if isinstance(s, dict) and s.get("creatable"):
                    item[sub] = sent
                    expects.append((f"items.{sub}", kind, want))
            body["items"] = [item]
        elif name == "tags" and tag_title:
            body["tags"] = [tag_title]
            expects.append(("tags", "tag", tag_title))
        elif name == "project":
            pid = (sample.get("project") or {}).get("id") or ""
            if pid:
                body["project"] = {"id": pid}
                expects.append(("project", "ref", pid.split("_", 1)[1] if "_" in pid else pid))
        elif name == "note":
            body["note"] = "verify-suite temp — safe to delete"
            expects.append(("note", "eq", body["note"]))
        elif name == "costCenter":
            body["costCenter"] = "VT-CC"
            expects.append(("costCenter", "eq", "VT-CC"))
        elif name == "currency":
            body["currency"] = sample.get("currency") or "EUR"
            expects.append(("currency", "eq", body["currency"]))
        elif name == "taxation":
            body["taxation"] = "domestic"
            expects.append(("taxation", "eq", "domestic"))
    return body, expects


def _simple_create_payload(
    key: str,
    schema: dict[str, Any],
    product_id: str | None,
    supplier_id: str | None,
    warehouse_id: str | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, Any]]]:
    """Create body + per-path persistence expectations for the price/product master
    entities (Product / PriceList / PurchasePrice), which take a create+verify probe
    outside the document/partner path. The Product body deliberately sets the numeric
    and boolean fields the null-restore update roundtrip cannot cover (minimumStock,
    hidePrice, …) so a silently-dropped field surfaces as create=fail."""
    prd = {"id": f"prd_{product_id}"} if product_id else None
    sup = {"id": f"sup_{supplier_id}"} if supplier_id else None
    if key == "Warehouse":
        # A DELETED warehouse keeps its designation reserved, so a fixed name answers
        # 409 "Warehouse already exists" on the second run — measured. Each probe
        # therefore names its own.
        label = f"VT Verify Lager {secrets.token_hex(3)}"
        return {"name": label}, [("name", "eq", label)]
    if key == "StorageLocation":
        # A location is created underneath its warehouse; without one upstream has
        # no endpoint at all, so the probe needs a real warehouse to hang it on.
        if not warehouse_id:
            return {}, []
        return (
            # No _MARK here: upstream validates the designation format and rejects
            # the marker's middle dot with "designation format is invalid".
            {"name": "VT-VERIFY-PROBE", "warehouse": {"id": f"wh_{warehouse_id}"}},
            [("name", "eq", "VT-VERIFY-PROBE")],
        )
    if key == "Task":
        return {
            "title": "VT Verify Aufgabe" + _MARK,
            "description": "VT verify description",
            "priority": "high",
            "status": "open",
            "dates": {"due": "2026-09-01", "dueTime": "14:30:00", "isAllDay": False},
            "recurrence": {"interval": "weekly"},
            "visibility": {"isPublic": True, "onStartPage": True, "onBulletinBoard": False},
            "reminder": {"byEmail": True, "daysBefore": 3, "advanceNoticeDays": 2},
            "timeTracking": {"hours": "2.50", "required": True, "billable": True},
            "notes": "VT verify note",
        }, [
            ("title", "eq", "VT Verify Aufgabe" + _MARK),
            ("description", "eq", "VT verify description"),
            ("priority", "eq", "high"),
            ("status", "eq", "open"),
            ("notes", "eq", "VT verify note"),
            ("dates.due", "eq", "2026-09-01"),
            ("dates.dueTime", "eq", "14:30:00"),
            ("dates.isAllDay", "eq", False),
            ("recurrence.interval", "eq", "weekly"),
            ("visibility.isPublic", "eq", True),
            ("visibility.onStartPage", "eq", True),
            ("reminder.byEmail", "eq", True),
            ("reminder.daysBefore", "eq", 3),
            ("reminder.advanceNoticeDays", "eq", 2),
            ("timeTracking.hours", "num", "2.50"),
            ("timeTracking.required", "eq", True),
            ("timeTracking.billable", "eq", True),
        ]
    if key == "CustomerGroup":
        # A price group, not a plain customer group: upstream refuses every
        # conditions field on `customerGroup`, so probing that kind would leave
        # the whole conditions block untested.
        return {
            "name": "VT Verify Preisgruppe" + _MARK,
            "kind": "priceGroup",
            "code": "VTVERIFY",
            "isActive": True,
            "internalNote": "VT verify note",
            "conditions": {
                "baseDiscount": "5.00",
                "paymentTermDays": 30,
                "cashDiscountRate": "2.00",
                "cashDiscountDays": 10,
                "freeShippingFrom": "150.00",
                "freeShippingActive": True,
            },
        }, [
            ("name", "eq", "VT Verify Preisgruppe" + _MARK),
            ("kind", "eq", "priceGroup"),
            ("code", "eq", "VTVERIFY"),
            ("isActive", "eq", True),
            ("internalNote", "eq", "VT verify note"),
            ("conditions.baseDiscount", "num", "5.00"),
            ("conditions.paymentTermDays", "eq", 30),
            ("conditions.cashDiscountRate", "num", "2.00"),
            ("conditions.cashDiscountDays", "eq", 10),
            ("conditions.freeShippingFrom", "num", "150.00"),
            ("conditions.freeShippingActive", "eq", True),
        ]
    if key == "PurchaseInvoice":
        # A real accounting document, so this only runs because DELETE is supported
        # and verified — the probe books it and takes it straight back out. Upstream
        # requires nothing at all (an empty POST creates an invoice), so everything
        # here is checking OUR mapping, including the line-item `actionIndicator`
        # diff that no schema documents.
        if not sup:
            return {}, []
        body = {
            "supplier": sup,
            "references": {"supplierInvoiceNumber": "VT-VERIFY" + _MARK},
            "dates": {"invoiceDate": "2026-01-15", "received": "2026-01-16"},
            "payment": {"dueDate": "2026-02-15", "discountUntil": "2026-01-25"},
            "currency": "EUR",
            "clarification": {"needed": True, "reason": "VT verify"},
            "items": [
                {
                    "name": "VT Verify Position",
                    "description": "VT verify line",
                    "quantity": {"value": 2, "unit": "Stk"},
                    "unitPrice": {"amount": "15.00", "currency": "EUR"},
                    "taxRate": 19,
                }
            ],
        }
        return body, [
            ("supplier", "ref", sup["id"]),
            ("references.supplierInvoiceNumber", "eq", "VT-VERIFY" + _MARK),
            ("dates.invoiceDate", "eq", "2026-01-15"),
            ("dates.received", "eq", "2026-01-16"),
            ("payment.dueDate", "eq", "2026-02-15"),
            ("payment.discountUntil", "eq", "2026-01-25"),
            ("currency", "eq", "EUR"),
            ("clarification.needed", "eq", True),
            ("clarification.reason", "eq", "VT verify"),
            ("items", "present", None),
        ]
    if key == "Product":
        body: dict[str, Any] = {
            "name": "VT Verify Product" + _MARK,
            "unit": "Stk",
            "description": "VT verify description",
            "prices": {
                "purchase": {"amount": "3.30", "currency": "EUR"},
                "sale": {"amount": "9.90", "currency": "EUR"},
            },
            "tax": {"rate": "standard"},
            "identifiers": {"hsCode": "49019900", "countryOfOrigin": "DE"},
            "manufacturer": {"name": "VT Mfr", "website": "https://vt.example"},
            "logistics": {
                "weight": {"value": 0.2, "unit": "kg"},
                "minimumOrderQuantity": 2,
                "minimumStockQuantity": 25,
                "dimensions": {"length": 10, "width": 5, "height": 3, "unit": "cm"},
            },
            "tracking": {"stock": True, "batches": False, "bestBefore": False},
            "production": {"mode": "none", "hasBillOfMaterials": False},
            "documentDefaults": {"hidePrice": True},
        }
        expects: list[tuple[str, str, Any]] = [
            ("name", "eq", body["name"]),
            ("unit", "eq", "Stk"),
            ("description", "eq", "VT verify description"),
            ("prices.purchase.amount", "eq", "3.30"),
            ("prices.sale.amount", "eq", "9.90"),
            ("tax.rate", "eq", "standard"),
            ("identifiers.hsCode", "eq", "49019900"),
            ("identifiers.countryOfOrigin", "eq", "DE"),
            ("manufacturer.name", "eq", "VT Mfr"),
            ("manufacturer.website", "eq", "https://vt.example"),
            ("logistics.weight.value", "num", 0.2),
            ("logistics.minimumOrderQuantity", "num", 2),
            ("logistics.minimumStockQuantity", "num", 25),
            ("logistics.dimensions.length", "num", 10),
            ("tracking.stock", "eq", True),
            ("production.mode", "eq", "none"),
            ("documentDefaults.hidePrice", "eq", True),
        ]
        return body, expects
    if key == "PriceList":
        if not prd:
            return {}, []
        body = {
            "product": prd,
            "minQuantity": 10,
            "unitPrice": {"amount": "7.50", "currency": "EUR"},
        }
        return body, [
            ("product", "ref", str(product_id)),
            ("minQuantity", "num", 10),
            ("unitPrice.amount", "eq", "7.50"),
        ]
    if key == "PurchasePrice":
        if not prd:
            return {}, []
        body = {
            "product": prd,
            "minQuantity": 10,
            "unitPrice": {"amount": "6.20", "currency": "EUR"},
            "isStandardSupplier": False,
        }
        expects = [
            ("product", "ref", str(product_id)),
            ("minQuantity", "num", 10),
            ("unitPrice.amount", "eq", "6.20"),
        ]
        if sup:
            body["supplier"] = sup
            expects.append(("supplier", "ref", str(supplier_id)))
        return body, expects
    return {}, []


class _AuthFailed(RuntimeError):
    """The run could not authenticate — not a fact about any capability.

    Raised rather than graded so it cannot be written down. A 401/403 used to be
    classified `pass` ("reachable — 401"), which meant an expired token painted a
    whole action suite green; grading it `fail` would be just as wrong in the other
    direction, since the next reader would take 62 red verdicts as evidence that the
    capabilities are broken and act on them.
    """


def _err(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "upstream error"
    title = payload.get("title") or payload.get("message") or "error"
    viol = payload.get("violations")
    if viol:
        return f"{title} {json.dumps(viol)[:160]}"
    return str(title)[:180]


def _auth() -> tuple[str, str]:
    from thirdpartytools.xentral.base import config as xcfg

    sanctum = os.environ.get("XENTRAL_API_KEY")
    # Local fast path: pin the base URL directly (e.g. XENTRAL_BASE_URL=
    # https://mvp.xentral.biz). Combined with the Sanctum key this needs no
    # Auth0 token at all — the Instance-Manager URL lookup returns an Okta login
    # page for locally-connected accounts, which breaks resolution. Skips that
    # whole path.
    base_override = os.environ.get("XENTRAL_BASE_URL")
    if base_override and sanctum:
        return base_override.rstrip("/"), sanctum

    from auth.token_manager import AuthTokenManager

    resolve_tok = AuthTokenManager().get_xentral_token_sync(_INSTANCE)
    base = xcfg.get_api_url_for_license(_INSTANCE, resolve_tok).rstrip("/")
    # Prefer the static Sanctum API key (XENTRAL_API_KEY, "id|hash") for the
    # entity/action routes: the Auth0 client-credentials token 500s on some
    # /api/entity and /api/v3 routes for locally-connected accounts. The base-url
    # resolution above still uses the Auth0 token (that part works).
    return base, (sanctum or resolve_tok)


async def _probe_action(
    adapter: Any, action_key: str, sample_id: str, base_url: str, token: str
) -> tuple[str | None, str | None]:
    """Probe one action / process-step command by invoking it through the facade.

    What the answer is WORTH, which is not the same as whether it arrived:

      executed   — 2xx: data changed, but this probe did not read the change back
      reachable  — the route exists and refused our deliberately empty command, on
                   validation (400/422) or on the record's state (409). Proof about
                   the route, no claim about the capability
      fail       — 404/405 (route gone), 501 (not wired in the facade), 5xx, crash
      None       — 409 wish (declared-but-not-upstream-yet; stays blue)

    ``pass`` is not reachable from here: seeing an effect needs a net-zero
    round-trip, which only ``_probe_tag_actions`` does. For `send` (mails a real
    customer) and `cancel` (mutates a document) it is not reachable at all on a live
    tenant — `executed` is their final answer, not a to-do.

    A 401/403 raises `_AuthFailed` instead of grading. An expired token is a broken
    RUN, not a property of the action: recording it per action would write 62
    capability-shaped verdicts that actually say "we could not log in", and the next
    reader would retire wishes over them.

    EXECUTES upstream: a no-input action (confirm, cancel …) will run on the sampled
    record. Sends an empty command so input-requiring actions get rejected at
    validation rather than committing."""
    envelope = json.dumps({"ids": [sample_id] if sample_id else [], "command": {}}).encode()
    try:
        resp = await adapter.action(
            action_key=action_key,
            handle=sample_id or None,
            body=envelope,
            base_url=base_url,
            token=token,
        )
    except Exception as exc:  # noqa: BLE001 - one probe must not kill the run
        return "fail", f"probe crashed: {exc}"
    st = resp.status_code
    try:
        payload = json.loads(resp.content or b"{}")
    except ValueError:
        payload = {}
    if st in (401, 403):
        raise _AuthFailed(f"{action_key} answered {st}: {_err(payload)}")
    if st == 409 and isinstance(payload, dict) and payload.get("wish"):
        return None, None
    if 200 <= st < 300:
        return "executed", f"EXECUTED on the sampled record ({st}) — effect not read back"
    if st == 501:
        return "fail", "not wired to an upstream action in the facade (501)"
    if st in (404, 405) or st >= 500:
        return "fail", f"route not available ({st}): {_err(payload)}"
    # The core's own input validator answers before the upstream is ever called, so
    # its refusal proves the facade route exists but says nothing about upstream.
    # Marked separately rather than guessed from the message text.
    source = payload.get("source") if isinstance(payload, dict) else None
    where = "the core's own validator" if source == "core" else "upstream"
    return "reachable", f"reachable — {st} from {where}: {_err(payload)}"


async def _probe_download_pdf(
    adapter: Any, sample_id: str, base_url: str, token: str
) -> tuple[str | None, str | None]:
    """`downloadPdf` is the one action whose effect is free to check.

    It renders a document and changes nothing, so unlike `send` or `cancel` the
    probe can look at what came back instead of trusting the status.

    The action answers a JSON envelope, not raw bytes: the rendered document rides
    in ``result.file.contentBase64`` next to ``filename``/``contentType``/
    ``sizeBytes``. Checking the HTTP body for ``%PDF-`` therefore fails on a
    perfectly good render — measured on mvp, it painted all seven document adapters
    red before this was corrected. Decode the field and check THAT: a 200 whose
    payload is not a PDF means the route answered without rendering, which the
    generic probe grades `executed`.
    """
    envelope = json.dumps({"ids": [sample_id], "command": {}}).encode()
    try:
        resp = await adapter.action(
            action_key="downloadPdf",
            handle=sample_id,
            body=envelope,
            base_url=base_url,
            token=token,
        )
    except Exception as exc:  # noqa: BLE001 - one probe must not kill the run
        return "fail", f"probe crashed: {exc}"
    if resp.status_code in (401, 403):
        raise _AuthFailed(f"downloadPdf answered {resp.status_code}")
    if not 200 <= resp.status_code < 300:
        return await _probe_action(adapter, "downloadPdf", sample_id, base_url, token)
    try:
        envelope = json.loads(resp.content or b"{}")
    except ValueError:
        return "fail", f"answered {resp.status_code} with a body that is not JSON"
    file_part = (
        ((envelope.get("result") or {}).get("file") or {}) if isinstance(envelope, dict) else {}
    )
    try:
        rendered = base64.b64decode(file_part.get("contentBase64") or "")
    except (ValueError, TypeError):
        return "fail", "result.file.contentBase64 is not decodable base64"
    if rendered.startswith(b"%PDF-"):
        return PROVEN, (
            f"rendered {file_part.get('filename') or 'a document'} "
            f"({len(rendered)} bytes of real PDF) — read-only, net-zero"
        )
    return "fail", (
        f"answered {resp.status_code} but result.file carries no PDF "
        f"(contentType {file_part.get('contentType')!r}, {len(rendered)} bytes)"
    )


async def _probe_tag_actions(
    adapter: Any, sample_id: str, base_url: str, token: str
) -> dict[str, tuple[str, str]]:
    """Effect-checked addTag/removeTag probe with a BRAND-NEW title.

    The generic empty-command probe only proves reachability, and the field
    suite's tags roundtrip deliberately reuses an existing catalogue tag — that
    combination masked the silent-drop bug (older builds answer 200 on a v3
    tags write without attaching a title missing from the catalogue). This
    probe exercises the actual contract ("created automatically if new"):
    addTag <fresh title> → verify attached → removeTag → verify gone → delete
    the auto-created catalogue tag. Net-zero; the catalogue stays unpolluted."""
    title = f"verify-probe-{secrets.token_hex(3)}"

    async def act(key: str) -> tuple[int, Any, list[str]]:
        resp = await adapter.action(
            action_key=key,
            handle=sample_id,
            body=json.dumps({"ids": [sample_id], "command": {"title": title}}).encode(),
            base_url=base_url,
            token=token,
        )
        try:
            payload = json.loads(resp.content or b"{}")
        except ValueError:
            payload = {}
        tags = (payload.get("data") or {}).get("tags") if isinstance(payload, dict) else None
        return resp.status_code, payload, [t for t in (tags or []) if isinstance(t, str)]

    out: dict[str, tuple[str, str]] = {}
    st, pl, tags = await act("addTag")
    if st >= 400:
        out["addTag"] = ("fail", f"addTag with fresh title → {st}: {_err(pl)}")
        return out
    if title not in tags:
        out["addTag"] = (
            "fail",
            f"200 but fresh title '{title}' is not on the record — unknown tags "
            "are silently dropped instead of auto-created",
        )
        return out
    out["addTag"] = ("pass", f"fresh title '{title}' attached — auto-create verified, net-zero")
    st, pl, tags = await act("removeTag")
    if st < 400 and title not in tags:
        out["removeTag"] = ("pass", "fresh title removed again — effect verified, net-zero")
    else:
        out["removeTag"] = (
            "fail",
            f"removeTag → {st}, title still present: {_err(pl)}"
            if st < 400
            else f"removeTag with fresh title → {st}: {_err(pl)}",
        )
    await _delete_catalogue_tag(adapter, title, base_url, token)
    return out


async def _delete_catalogue_tag(adapter: Any, title: str, base_url: str, token: str) -> None:
    """Remove the probe's auto-created tag from the BF catalogue (net-zero).
    Scans the first 100 rows — real catalogues are small; a miss only leaves a
    stray probe tag behind, it never fails the run."""
    headers = adapter._headers(token, None)
    url = f"{base_url.rstrip('/')}/api/entity/tag"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(url, params={"page[size]": "100"}, headers=headers)
            for row in (r.json().get("data") or []) if r.status_code == 200 else []:
                if row.get("label") == title and (row.get("uuid") or row.get("id")):
                    await c.delete(f"{url}/{row.get('uuid') or row.get('id')}", headers=headers)
                    return
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        print(f"    (catalogue cleanup for '{title}' failed: {exc})")


class _Probe:
    def __init__(self, adapter: Any, base_url: str, token: str) -> None:
        self.a, self.base, self.tok = adapter, base_url, token

    async def req(
        self,
        method: str = "GET",
        handle: str | None = None,
        query: list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        resp = await self.a.request(
            method=method,
            handle=handle,
            query=query or [],
            body=json.dumps(body).encode() if body is not None else None,
            base_url=self.base,
            token=self.tok,
        )
        try:
            return resp.status_code, json.loads(resp.content or b"{}")
        except ValueError:
            return resp.status_code, {}


async def _verify_entity(
    adapter: Any,
    base_url: str,
    token: str,
    product_id: str | None = None,
    tag_title: str | None = None,
    supplier_id: str | None = None,
    warehouse_id: str | None = None,
) -> tuple[dict | None, str]:
    p = _Probe(adapter, base_url, token)
    key = adapter.manifest.key
    st, payload = await p.req(query=[("page[size]", "10")])
    rows = payload.get("data") or []
    if st in (401, 403):
        # Not "this entity is unreadable" — the run itself is not authenticated. The
        # old path returned "left untested", kept every previous block, and still
        # rewrote generatedAt, dating stale data as fresh.
        raise _AuthFailed(f"{key} list read answered {st}: {_err(payload)}")
    if st != 200 or not rows:
        return None, f"read {st}, {len(rows)} rows — left untested"
    schema = adapter.fields()
    fields: dict[str, dict[str, Any]] = {
        pth: {"read": "unobserved"} for pth in _field_paths(schema)
    }

    # A status value the entity's map does not know is reported as the entity's
    # default — silently. This scan exists to catch that, and it reads its OWN wide
    # page: the ten sampled rows are luck of the draw, and on mvp the first ten
    # delivery notes happen to carry no `sent` at all, which is exactly the value
    # that was missing. One extra list call per entity buys the coverage.
    STATUS_FALLBACKS.clear()
    _, wide_payload = await p.req(query=[("page[size]", "100")])
    status_misses = sorted(STATUS_FALLBACKS)

    # The corpus every `read` verdict is decided against. The wide page was already
    # being fetched and thrown away; keeping it costs nothing and is the difference
    # between "we saw 10 records" and "we saw 100".
    seen_records: list[Any] = list(rows) + list(wide_payload.get("data") or [])

    # Report them on the field that carries the status. Which one that is differs
    # per entity (`status`, `progress`, …), so it is found by its declared options:
    # the field whose vocabulary contains the value that was substituted.
    for raw_value, used in status_misses:
        target = next(
            (
                name
                for name, spec in schema.items()
                if isinstance(spec, dict)
                and any((o or {}).get("value") == used for o in spec.get("options") or [])
            ),
            None,
        )
        if target:
            fields.setdefault(target, {})["read"] = "fail"
            fields[target]["readNote"] = (
                f"upstream sent {raw_value!r}, which the entity's status map does not "
                f"know — reported as {used!r} instead"
            )

    detail_only = tuple(getattr(adapter, "detail_only_sections", ()) or ())

    def is_detail_only(path: str) -> bool:
        return any(path == d or path.startswith(d + ".") for d in detail_only)

    detail_cache: dict[str, dict[str, Any]] = {}

    async def detail_view(handle: str, fallback: dict[str, Any]) -> dict[str, Any]:
        if handle not in detail_cache:
            dst, dpl = await p.req(handle=handle)
            detail_cache[handle] = (dpl.get("data") or {}) if dst == 200 else fallback
        return detail_cache[handle]

    # a sample with the most non-null values gives the probes more ammunition
    sample = max(
        rows, key=lambda r: sum(1 for v in (r or {}).values() if v not in (None, "", [], {}))
    )

    # Detail-only sections come from a sub-resource the adapter calls only on a
    # single read, so every list row carries null for them. Grading those against
    # the list alone would report the whole section unobserved, which is a statement
    # about this probe rather than about the upstream — so fetch the sample once and
    # judge them on that. (The same nulls used to make every write to such a field
    # read back as "accepted but not persisted".)
    if detail_only and (handle := sample.get("id")):
        seen_records.append(await detail_view(str(handle), sample))

    # `read` is now a measurement, not a restatement of the schema. Every declared
    # path starts `unobserved` and is raised only where a record actually carried a
    # value; 1218 paths used to be stamped `pass` before any payload was read.
    #
    # `unobserved` is NOT a failure and must never be rendered as one — mvp is a
    # narrow instance, and a field nobody there fills is simply unproven either way.
    # The status-map misses above are the exception: those were observed and WRONG,
    # so their `fail` outranks anything measured here.
    for pth, facets in fields.items():
        if facets.get("read") == "fail":
            continue
        if _observed(seen_records, pth):
            facets["read"] = PROVEN
            continue
        if is_detail_only(pth):
            facets["readNote"] = (
                "detail-only section: read on the single record too, and no value "
                "there either — a list always leaves it null and names the section "
                "in extra.unavailableSections"
            )
        elif "." in pth and not _observed(seen_records, pth.rsplit(".", 1)[0]):
            facets["readNote"] = (
                f"no sampled record carries {pth.rsplit('.', 1)[0]!r}, so the leaf "
                "could not be observed — nothing proven either way"
            )
        else:
            facets["readNote"] = (
                f"declared, but none of the {len(seen_records)} sampled records "
                "carries a value — nothing proven either way"
            )
    # The fields a consolidated search actually reaches — the upstream's own set
    # where it searches natively, the fan-out's otherwise. A schema can flag a field
    # `searchable` without it being in either, and that gap is real: a declared leaf
    # nothing searches is a promise the entity does not keep, so the probe reports it
    # rather than papering over it.
    searchable_fields = set(
        getattr(adapter, "native_search_fields", ())
        or getattr(adapter, "search_fields", lambda: ())()
    )

    counts: dict[str, collections.Counter[str]] = {
        facet: collections.Counter() for facet in ("filter", "sort", "search", "update", "create")
    }

    def mark(
        path: str, facet: str, ok: bool, note: str | None = None, *, when_ok: str = PROVEN
    ) -> None:
        """``when_ok`` is what a success is WORTH, not merely that there was one.

        Callers that assert an effect leave it at `pass`; callers that only saw a
        request succeed pass `accepted`, so the difference survives into the file
        instead of being flattened into one green.
        """
        f = fields.setdefault(path, {})
        verdict = when_ok if ok else "fail"
        f[facet] = verdict
        if note:
            f[f"{facet}Note"] = note[:300]
        elif not ok:
            f[f"{facet}Note"] = "probe failed"
        counts[facet][verdict] += 1

    for path, spec in _walk(schema):
        # The probe value has to come through collections too, or a leaf under one
        # can never be filtered or searched for want of something to look for — that
        # is why the five `addresses.*` leaves read "no sample value" for as long as
        # they existed, `_value_at` stopping at the first list.
        #
        # The OWNER travels with the value. Both the filter and the search check
        # assert that the record the value came from comes back, and widening the
        # search beyond `sample` without carrying its record would make that anchor
        # point at the wrong row — turning a working filter into a false `accepted`.
        val, val_owner = None, sample
        for _rec in (sample, *seen_records):
            _picked = next((v for v in _values_at(_rec, path) if _is_filled(v)), None)
            if _picked is not None:
                val, val_owner = _picked, _rec
                break
        if spec.get("filterable"):
            fval = val.get("id") if isinstance(val, dict) else val
            if isinstance(fval, str) and "_" in fval and spec.get("type") == "reference":
                fval = fval.split("_", 1)[1]
            # A datetime filter is probed in BOTH representations, because the two
            # endpoint families disagree and neither can be guessed from the schema
            # (measured on mvp): /api/v3/customers rejects a full timestamp with
            # "not a valid date" and wants Y-m-d, while /api/v3/salesOrders rejects
            # Y-m-d with "not a valid datetime" and wants the full one. Hard-coding
            # either form paints half the entities red — this file has now done it
            # in both directions. Pass = one of them is accepted; the note records
            # which, so the asymmetry stays visible instead of being smoothed over.
            if isinstance(val, list):
                fval = None  # collections (tags) need a scalar probe value —
                # the chosen sample row often has none, so scan ALL fetched rows
                for row in [sample, *rows]:
                    items = _value_at(row, path)
                    for item in items if isinstance(items, list) else []:
                        if isinstance(item, str) and item:
                            fval = item
                            break
                    if fval:
                        break
                # No tagged row on this instance: probe with the catalogue tag
                # fixture. Upstream rejects unknown filter KEYS with a 400, so
                # a 200 (even with zero hits) proves the key is accepted.
                if fval is None and path == "tags" and tag_title:
                    fval = tag_title
            if fval in (None, "", [], {}):
                # NOTE: no `continue` — the sort/search probes below must still
                # run for this field even without a filter sample value.
                fields.setdefault(path, {})["filterNote"] = (
                    "no sample value on the test instance — filter not probed"
                )
            else:
                # Booleans have to go out lowercase — str(False) is "False" and
                # upstream answers "Invalid value: False. Valid values are: true,
                # false". This probe builds its own query rather than going through
                # the MCP tool, so it needs the same treatment the tool got
                # (agent-os: service.filter_value).
                if isinstance(fval, bool):
                    candidates = ["true" if fval else "false"]
                else:
                    candidates = [str(fval)]
                    if spec.get("type") == "datetime" and "T" in str(fval):
                        candidates.append(str(fval).split("T", 1)[0])

                fst, fpl, used = 0, {}, ""
                for candidate in candidates:
                    fst, fpl = await p.req(
                        query=[
                            ("page[size]", "5"),
                            ("filter[0][key]", path),
                            ("filter[0][op]", "equals"),
                            ("filter[0][value]", candidate),
                        ]
                    )
                    used = candidate
                    if fst == 200:
                        break
                note = None
                when_ok = "accepted"
                if fst != 200:
                    note = f"filter[{path}] {fst}: {_err(fpl)}"
                elif len(candidates) > 1 and used != candidates[0]:
                    note = "accepts the date part only, not the full timestamp it returns on read"
                else:
                    # The value came off a sampled record, so that record MUST come
                    # back. Several v3 list endpoints answer 200 while ignoring the
                    # filter entirely (the facade guards against undeclared keys for
                    # exactly this reason) — a status check alone grades that green.
                    hits = fpl.get("data") or []
                    anchor = (val_owner or {}).get("id")
                    if any((h or {}).get("id") == anchor for h in hits):
                        when_ok = PROVEN
                    elif not hits:
                        note = "accepted, but returned nothing — the record the value came from is missing"
                        when_ok = "accepted"
                    else:
                        note = (
                            "accepted, but the page does not contain the record the value "
                            "came from — the filter may have been ignored"
                        )
                mark(path, "filter", fst == 200, note, when_ok=when_ok)
        if spec.get("sortable"):
            desc_st, desc_pl = await p.req(query=[("page[size]", "5"), ("sort", f"-{path}")])
            if desc_st != 200:
                mark(path, "sort", False, f"sort -{path} {desc_st}: {_err(desc_pl)}")
            else:
                asc_st, asc_pl = await p.req(query=[("page[size]", "5"), ("sort", path)])
                verdict, note = _sort_effect(path, desc_pl, asc_pl, asc_st)
                mark(path, "sort", verdict != "fail", note, when_ok=verdict)
        if spec.get("searchable"):
            # The facade recognises a search only as `filter[i][key]=search`, and
            # fans it out over `search_fields()` as a per-field `contains` filter.
            # The old probe sent a bare `?search=` param, which reaches none of that
            # and went to the upstream verbatim. What that proved depends on the
            # endpoint, and the earlier note here got it wrong by generalising:
            # v3 has a NATIVE `?search=` on the nine document endpoints, so those
            # 200s were a real search — while on customers/suppliers/products the
            # parameter is silently ignored (measured: `?search=nonsense` answers 200
            # with all 20128 customers), so those 200s proved nothing at all. Either
            # way it was measuring the upstream rather than the contract consumers
            # use, so it says nothing about THIS field. Here the probe sends what
            # `build_field_query` builds and asserts the record the value came from
            # comes back.
            if path not in searchable_fields:
                mark(
                    path,
                    "search",
                    False,
                    "declared searchable, but not part of this entity's search fan-out — "
                    f"searchFields is {sorted(searchable_fields) or 'empty'}, so a consolidated "
                    "search never looks at this field",
                )
                continue
            sval = val if isinstance(val, str) and val else None
            fixture_used = False
            if not sval:
                sval = _SEARCH_FIXTURES.get(path.rsplit(".", 1)[-1])
                fixture_used = sval is not None
            if not sval:
                fields.setdefault(path, {})["searchNote"] = (
                    "no sample text value and no fixture — search not probed"
                )
                continue
            sst, spl = await p.req(
                query=[
                    ("page[size]", "20"),
                    ("filter[0][key]", "search"),
                    ("filter[0][op]", "contains"),
                    ("filter[0][value]", sval[:40]),
                ]
            )
            if sst != 200:
                mark(path, "search", False, f"search {sst}: {_err(spl)}")
            elif fixture_used:
                # A fixture value belongs to no sampled record, so there is no anchor
                # to look for — the request working is all this can show.
                mark(
                    path,
                    "search",
                    True,
                    "probed with a fixture value — no sampled record carries this field, "
                    "so a hit could not be asserted",
                    when_ok="accepted",
                )
            elif any(
                (h or {}).get("id") == (val_owner or {}).get("id") for h in (spl.get("data") or [])
            ):
                mark(path, "search", True, None)
            else:
                # The fan-out ORs over every searchable field, so a miss here does not
                # single this field out — it says the consolidated search did not find
                # a record that demonstrably carries the term.
                mark(
                    path,
                    "search",
                    True,
                    "the consolidated search accepted the term but did not return the "
                    "record it was taken from",
                    when_ok="accepted",
                )

    # update roundtrip (net-zero) on EVERY updatable scalar leaf (top-level and
    # nested: texts.intro, dates.issued, billingAddress.street …). Address parents
    # are written as the FULL object with one leaf changed so a partial write
    # cannot clear the siblings. Sampled records are often write-protected (old
    # completed documents) — prefer DRAFT records fetched via the status filter,
    # fall back to the sampled rows, and remember the first row that accepts.
    if "update" in adapter.manifest.operations:
        update_rows = list(rows)
        if isinstance(schema.get("status"), dict) and schema["status"].get("filterable"):
            dst, dpl = await p.req(
                query=[
                    ("page[size]", "5"),
                    ("filter[0][key]", "status"),
                    ("filter[0][op]", "equals"),
                    ("filter[0][value]", "draft"),
                ]
            )
            drafts = (dpl.get("data") or []) if dst == 200 else []
            if drafts:
                update_rows = drafts + rows
        preferred: dict[str, Any] | None = None
        for path, spec in _update_targets(schema):
            last_note = "no updatable record found"
            done = False
            skip_note: str | None = None
            row_order = ([preferred] if preferred else []) + [
                r for r in update_rows[:8] if r is not preferred
            ]
            for row in row_order:
                handle = str(row.get("id") or "")
                if not handle:
                    continue
                view = await detail_view(handle, row) if is_detail_only(path) else row
                orig = _value_at(view, path)
                leaf = path.rsplit(".", 1)[-1]
                if spec.get("type") == "date":
                    if not isinstance(orig, str) or not orig:
                        skip_note = (
                            "no sample date value — not probed (a null date cannot be restored)"
                        )
                        continue
                    testv: Any = "2026-02-02" if orig != "2026-02-02" else "2026-03-03"
                elif spec.get("type") == "boolean":
                    if not isinstance(orig, bool):
                        skip_note = (
                            "no sample boolean value — not probed (a null flag cannot be restored)"
                        )
                        continue
                    testv = not orig
                elif spec.get("type") in ("integer", "decimal", "number"):
                    if not isinstance(orig, (int, float)) or isinstance(orig, bool):
                        skip_note = "no sample numeric value — not probed (a null number cannot be restored)"
                        continue
                    testv = orig + 1
                elif leaf in _VALID_TOGGLE:
                    a, b = _VALID_TOGGLE[leaf]
                    testv = a if orig != a else b
                elif (alt := _option_toggle(spec, orig)) is not None:
                    # A constrained value set (select/enum): toggle to another
                    # DECLARED option. Appending the marker would send something
                    # the field cannot hold, and "accepted but not persisted" would
                    # then say nothing about writability.
                    testv = alt
                elif (num := _numeric_string_toggle(orig)) is not None:
                    # Money and other numbers carried as strings ("4.00"). The
                    # marker made these "4.00·vt" — rejected outright by the
                    # stricter endpoints and silently dropped by the lax ones, and
                    # on one lax endpoint it PERSISTED, which is what later crashed
                    # the PurchasePrice probe on read-back.
                    testv = num
                elif (clock := _time_string_toggle(orig)) is not None:
                    # A wall-clock string. The marker turns "00:00:00" into
                    # something upstream accepts with a 200 and silently
                    # normalises back to midnight — a writable field then reads as
                    # "accepted but did not persist" forever.
                    testv = clock
                else:
                    # NEVER toggle to/from empty: upstream IGNORES empty-string
                    # writes, so an ""-restore silently leaves the marker behind
                    # and the persistence check false-fails. Both toggle values
                    # must be non-empty; empty originals restore via " " (which
                    # upstream trims back to empty).
                    base_txt = orig if isinstance(orig, str) else ""
                    clean = base_txt.replace(_MARK, "").strip()
                    if clean:
                        testv = clean + _MARK if base_txt != clean + _MARK else clean
                    else:
                        testv = "VT" + _MARK
                top = path.split(".")[0]
                restore_leaf: Any = orig
                if isinstance(orig, str | type(None)) and not (orig or "").strip():
                    restore_leaf = " "  # trimmed to empty upstream
                if top.endswith("Address") and "." in path:
                    had_parent = bool(row.get(top))
                    parent = dict(row.get(top) or {})
                    if not had_parent:
                        # creating an address from scratch needs its required
                        # name — a lone sub-field 200s but silently no-ops
                        parent["name"] = _ADDR_CREATE["name"]
                    parent[path.split(".", 1)[1]] = testv
                    body: dict[str, Any] = {top: parent}
                    if had_parent:
                        rparent = dict(row.get(top) or {})
                        rparent[path.split(".", 1)[1]] = restore_leaf
                        restore: dict[str, Any] = {top: rparent}
                    else:
                        restore = {top: None}  # clear the probe-created address
                else:
                    body = _nested_body(path, testv)
                    restore = _nested_body(path, restore_leaf)
                ust, upl = await p.req("PATCH", handle, body=body)
                if ust >= 400:
                    last_note = f"PATCH {ust}: {_err(upl)}"
                    continue  # e.g. write-protected — try the next row
                if is_detail_only(path):
                    detail_cache.pop(handle, None)  # the write just changed it
                    _ast, apl = await p.req(handle=handle)
                    after = _value_at(apl.get("data") or {}, path)
                else:
                    after = _value_at(upl.get("data") or {}, path)
                if (
                    isinstance(testv, (int, float))
                    and not isinstance(testv, bool)
                    and isinstance(after, (int, float))
                    and not isinstance(after, bool)
                ):
                    took = abs(float(after) - float(testv)) < 1e-6
                else:
                    took = after == testv
                mark(
                    path,
                    "update",
                    took,
                    None if took else "upstream accepted the write but the value did not persist",
                )
                await p.req("PATCH", handle, body=restore)
                detail_cache.pop(handle, None)
                preferred = row
                done = True
                break
            if not done:
                if skip_note and last_note == "no updatable record found":
                    fields.setdefault(path, {})["updateNote"] = skip_note
                else:
                    mark(path, "update", False, last_note)

        # LINE ITEMS. _update_targets skips collections on purpose — a document's
        # positions are not written by PATCHing the whole list, the core reconciles
        # them against the v3 lineItems sub-resource one line at a time. But
        # skipping them left every line field `offen`, purchasePrice included: the
        # field this suite was extended for was the one it never measured. So probe
        # a single line the way the core writes one, and read the result back off
        # the document.
        items_spec = schema.get("items")
        if isinstance(items_spec, dict) and items_spec.get("type") == "collection":
            item_props = (items_spec.get("node") or {}).get("properties") or {}
            line_targets = [
                (n, sp)
                for n, sp in item_props.items()
                if isinstance(sp, dict) and sp.get("updatable")
            ]
            carrier = next((r for r in update_rows if r.get("id") and (r.get("items") or [])), None)
            for name, spec in line_targets:
                path = f"items.{name}"
                if carrier is None:
                    fields.setdefault(path, {})["updateNote"] = (
                        "no sampled record carries a line item — not probed"
                    )
                    continue
                handle = str(carrier["id"])
                line = (carrier.get("items") or [])[0]
                line_id = str(line.get("id") or "")
                orig = line.get(name)
                testv, restore_v = _line_toggle(spec, name, orig)
                if testv is None:
                    fields.setdefault(path, {})["updateNote"] = (
                        f"no sample value for {name} — not probed "
                        "(a null line value cannot be restored net-zero)"
                    )
                    continue
                ust, upl = await p.req(
                    "PATCH", handle, body={"items": [{"id": line_id, name: testv}]}
                )
                if ust >= 400:
                    mark(path, "update", False, f"PATCH {ust}: {_err(upl)}")
                    continue
                # Read the document back rather than trusting the write response:
                # the reconcile answers with its own re-read, and a line write that
                # never reached upstream would still echo.
                _gst, gpl = await p.req(handle=handle)
                after = next(
                    (
                        it.get(name)
                        for it in ((gpl.get("data") or {}).get("items") or [])
                        if str(it.get("id") or "") == line_id
                    ),
                    None,
                )
                took = _line_eq(after, testv)
                mark(
                    path,
                    "update",
                    took,
                    None if took else "upstream accepted the write but the value did not persist",
                )
                await p.req("PATCH", handle, body={"items": [{"id": line_id, name: restore_v}]})

        # reference update: switch project to another sampled project id, restore.
        pspec = schema.get("project")
        if isinstance(pspec, dict) and pspec.get("updatable"):
            proj_ids = []
            for row in update_rows:
                pid = (row.get("project") or {}).get("id") or ""
                if pid and pid not in proj_ids:
                    proj_ids.append(pid)
            target = preferred or next((r for r in update_rows if r.get("id")), None)
            if target and proj_ids:
                cur = (target.get("project") or {}).get("id") or ""
                alt = next((x for x in proj_ids if x != cur), cur or proj_ids[0])
                alt_up = alt.split("_", 1)[1] if "_" in alt else alt
                ust, upl = await p.req("PATCH", str(target["id"]), body={"project": {"id": alt}})
                if ust >= 400:
                    mark("project", "update", False, f"PATCH {ust}: {_err(upl)}")
                else:
                    got = (upl.get("data") or {}).get("project")
                    ok = _cmp("ref", got, alt_up)
                    note2 = None
                    if ok and alt == cur:
                        note2 = (
                            "only one project on the instance — write accepted, change not provable"
                        )
                    mark(
                        "project",
                        "update",
                        ok,
                        note2
                        if ok
                        else "upstream accepted the write but the value did not persist",
                    )
                    # restore (None clears the association)
                    await p.req(
                        "PATCH", str(target["id"]), body={"project": {"id": cur} if cur else None}
                    )
            else:
                fields.setdefault("project", {})["updateNote"] = (
                    "no project on any sampled record — reference switch not probed"
                )

        # tags roundtrip: toggle a catalogue tag on, verify, restore (net-zero).
        tspec = schema.get("tags")
        if isinstance(tspec, dict) and tspec.get("updatable") and tag_title:
            target = preferred or next((r for r in update_rows if r.get("id")), None)
            if target:
                orig_tags = [t for t in (target.get("tags") or []) if isinstance(t, str)]
                test_tags = (
                    orig_tags + [tag_title]
                    if tag_title not in orig_tags
                    else [t for t in orig_tags if t != tag_title]
                )
                ust, upl = await p.req("PATCH", str(target["id"]), body={"tags": test_tags})
                if ust >= 400:
                    mark("tags", "update", False, f"PATCH {ust}: {_err(upl)}")
                else:
                    got = (upl.get("data") or {}).get("tags") or []
                    ok = sorted(got) == sorted(test_tags)
                    mark(
                        "tags",
                        "update",
                        ok,
                        None
                        if ok
                        else "upstream accepted the tag write but the list did not persist",
                    )
                    await p.req("PATCH", str(target["id"]), body={"tags": orig_tags})

    # create + delete (documents): send EVERY creatable field the adapter accepts
    # (addresses, item subs, texts, dates, taxation, project, tags, …), verify each
    # path persisted on the created record, then DELETE it (net-zero). A sampled
    # partner id may no longer exist — try several before recording a failure.
    partner_field = _CREATE_PARTNER.get(key)
    writable = set(getattr(adapter, "_WRITABLE", ()) or ())
    if partner_field and "create" in adapter.manifest.operations:
        pids: list[str] = []
        for row in rows:
            pref = row.get(partner_field) or {}
            pid = pref.get("id") if isinstance(pref, dict) else None
            if pid and pid not in pids:
                pids.append(pid)
        if pids:
            note = "create not probed"
            created = False
            for pid in pids[:4]:
                body, expects = _doc_create_payload(schema, writable, sample, product_id, tag_title)
                body[partner_field] = {"id": pid}
                cst, cpl = await p.req("POST", body=body)
                if cst >= 400 and "items" in body and "lineItems" in _err(cpl):
                    # a single hard item mustn't fail the whole create — but only
                    # strip items when the error actually points at lineItems
                    # (other 400s, e.g. an invalid partner, retry with next pid).
                    fields.setdefault("items", {})["create"] = "fail"
                    fields["items"]["createNote"] = f"POST {cst}: {_err(cpl)}"
                    body.pop("items")
                    expects = [e for e in expects if not e[0].startswith("items")]
                    cst, cpl = await p.req("POST", body=body)
                data = (cpl.get("data") or {}) if isinstance(cpl, dict) else {}
                new_id = data.get("id")
                if cst in (200, 201) and new_id:
                    mark(partner_field, "create", data.get(partner_field) is not None)
                    for path, kind, want in expects:
                        ok = _cmp(kind, _got(data, path), want)
                        mark(
                            path,
                            "create",
                            ok,
                            None
                            if ok
                            else "sent on create but did not persist on the created record",
                        )
                    dst, _ = await p.req("DELETE", str(new_id))
                    if dst >= 400:
                        fields.setdefault("note", {})["createNote"] = (
                            f"created {new_id} but DELETE returned {dst} — manual cleanup needed"
                        )
                    created = True
                    break
                note = f"POST {cst}: {_err(cpl)}"
            if not created:
                for f in (partner_field, "note"):
                    mark(f, "create", False, note)
        else:
            fields.setdefault(partner_field, {})["createNote"] = (
                "no partner id in the sampled record — create not probed"
            )

    # master-data create + delete (Customer/Supplier): create a clearly-labelled
    # verify record with every creatable field, check persistence, DELETE it.
    if key in ("Customer", "Supplier") and "create" in adapter.manifest.operations:
        body = {}
        expects = []
        for fname, val in _MASTER_CREATE.items():
            spec2 = schema.get(fname)
            if isinstance(spec2, dict) and spec2.get("creatable"):
                body[fname] = val
                expects.append((fname, "eq", val))
        # The main address is the DEFAULT row of the unified addresses list (no more
        # separate primaryAddress block). Build it from the addresses node's creatable
        # geo leaves so the POST carries the required street/zip/city/country.
        addr_node = ((schema.get("addresses") or {}).get("node") or {}).get("properties") or {}
        main_addr = {
            k: _ADDR_CREATE[k]
            for k, s in addr_node.items()
            if isinstance(s, dict) and s.get("creatable") and k in _ADDR_CREATE
        }
        # partner sub-resources (docs/01-model.md §6.1): send the collections IN
        # the create body — the adapter creates the record, then syncs them.
        has_subres = isinstance(schema.get("contacts"), dict)
        if has_subres:
            body["contacts"] = [
                {
                    "name": "VT Kontakt",
                    "type": "mr",
                    "department": "Einkauf",
                    "position": "Leiter",
                    "email": "verify-kontakt@example.test",
                }
            ]
            body["addresses"] = [
                {"type": "both", "isDefault": True, "label": "Hauptsitz", **main_addr},
                {
                    "type": "shipping",
                    "name": "VT Lager",
                    "street": "Hafenweg 2",
                    "zip": "20457",
                    "city": "Hamburg",
                    "country": "DE",
                },
                {
                    "type": "billing",
                    "name": "VT Rechnung",
                    "street": "Postfach 12",
                    "zip": "86150",
                    "city": "Augsburg",
                    "country": "DE",
                },
            ]
        if body:
            cst, cpl = await p.req("POST", body=body)
            data = (cpl.get("data") or {}) if isinstance(cpl, dict) else {}
            new_id = data.get("id")
            if cst in (200, 201) and new_id:
                for path, kind, want in expects:
                    ok = _cmp(kind, _got(data, path), want)
                    mark(
                        path,
                        "create",
                        ok,
                        None if ok else "sent on create but did not persist on the created record",
                    )
                if has_subres:
                    cons = data.get("contacts") or []
                    adrs = data.get("addresses") or []
                    con_ok = any(c.get("name") == "VT Kontakt" for c in cons)
                    ship_ok = any(
                        a.get("type") == "shipping" and a.get("city") == "Hamburg" for a in adrs
                    )
                    bill_ok = any(a.get("type") == "billing" for a in adrs)
                    mark(
                        "contacts",
                        "create",
                        con_ok,
                        None if con_ok else "contact missing on created record",
                    )
                    mark(
                        "addresses",
                        "create",
                        ship_ok and bill_ok,
                        None if ship_ok and bill_ok else "address missing on created record",
                    )
                    # update roundtrip: full-set PATCH (mutate department + shipping city)
                    if cons and adrs:
                        cons2 = [dict(cons[0], department="Vertrieb")]
                        adrs2 = [
                            dict(a, city="Bremen") if a.get("type") == "shipping" else a
                            for a in adrs
                        ]
                        ust, upl = await p.req(
                            "PATCH", str(new_id), body={"contacts": cons2, "addresses": adrs2}
                        )

                        # Read the record back rather than trusting the PATCH echo —
                        # an echo reflects the request, not what was stored.
                        #
                        # A miss here is `accepted`, NOT `fail`. Measured on mvp over
                        # five consecutive identical runs: the contact's new
                        # department shows up on the read-back about half the time,
                        # and twice more with a second read — while the address
                        # change in the very same request is there every time. So the
                        # write works; what cannot be established in one run is that
                        # it took. Grading that red would flip the cell at random,
                        # which is the same lie as a false green, and retrying until
                        # it passes would bury the wobble instead of recording it.
                        def _has(data: dict[str, Any], key: str, field: str, want: str) -> bool:
                            return any(
                                (row or {}).get(field) == want for row in data.get(key) or []
                            )

                        udata: dict[str, Any] = {}
                        if ust == 200:
                            for _ in range(2):
                                _rst, rpl = await p.req(handle=str(new_id))
                                udata = (rpl.get("data") or {}) if isinstance(rpl, dict) else {}
                                if _has(udata, "contacts", "department", "Vertrieb"):
                                    break
                        ok_c = ust == 200 and _has(udata, "contacts", "department", "Vertrieb")
                        ok_a = ust == 200 and _has(udata, "addresses", "city", "Bremen")
                        if ust != 200:
                            mark("contacts", "update", False, f"PATCH {ust}: {_err(upl)}")
                        elif ok_c:
                            mark("contacts", "update", True)
                        else:
                            mark(
                                "contacts",
                                "update",
                                True,
                                "accepted, but the change was not visible on two "
                                "read-backs — upstream applies it late often enough "
                                "that a single run cannot assert it",
                                when_ok="accepted",
                            )
                        mark(
                            "addresses",
                            "update",
                            ok_a,
                            None if ok_a else f"PATCH {ust}: {_err(upl)}",
                        )
                dst, _ = await p.req("DELETE", str(new_id))
                if dst >= 400:
                    fields.setdefault("name", {})["createNote"] = (
                        f"created {new_id} but DELETE returned {dst} — manual cleanup needed "
                        f"(record is named 'VT Verify GmbH')"
                    )
            else:
                note3 = f"POST {cst}: {_err(cpl)}"
                for path, _k, _w in expects:
                    mark(path, "create", False, note3)

    # create + (delete) for the price/product master entities (Product, PriceList,
    # PurchasePrice): POST a rich body — including the numeric/boolean fields the
    # null-restore update roundtrip cannot reach — verify each path persisted, then
    # DELETE (net-zero) where supported. Product has NO delete: opt-in via
    # VERIFY_CREATE_NONZERO and the labelled record is left in place.
    _CREATE_PROBED = (
        "Product",
        "PriceList",
        "PurchasePrice",
        "Warehouse",
        "StorageLocation",
        "PurchaseInvoice",
        "Task",
        "CustomerGroup",
    )
    if key in _CREATE_PROBED and "create" in adapter.manifest.operations:
        can_delete = "delete" in adapter.manifest.operations
        if not can_delete and not _PROBE_CREATE_NONZERO:
            fields.setdefault("name", {})["createNote"] = (
                "create not probed — no delete endpoint (net-zero impossible); set "
                "VERIFY_CREATE_NONZERO=1 to probe and leave a labelled record"
            )
        else:
            body, expects = _simple_create_payload(
                key, schema, product_id, supplier_id, warehouse_id
            )
            if not body:
                fields.setdefault("name", {})["createNote"] = (
                    "create not probed — missing a product/supplier fixture on the instance"
                )
            else:
                cst, cpl = await p.req("POST", body=body)
                data = (cpl.get("data") or {}) if isinstance(cpl, dict) else {}
                new_id = data.get("id")
                if cst in (200, 201) and new_id:
                    for path, kind, want in expects:
                        ok = _cmp(kind, _got(data, path), want)
                        mark(
                            path,
                            "create",
                            ok,
                            None
                            if ok
                            else "sent on create but did not persist on the created record",
                        )
                    if can_delete:
                        dst, _ = await p.req("DELETE", str(new_id))
                        if dst >= 400:
                            fields.setdefault("name", {})["createNote"] = (
                                f"created {new_id} but DELETE returned {dst} — manual cleanup needed"
                            )
                    else:
                        fields.setdefault("name", {})["createNote"] = (
                            f"created {new_id} (labelled 'VT Verify') — no delete endpoint, "
                            "left in place"
                        )
                else:
                    for path, _k, _w in expects:
                        mark(path, "create", False, f"POST {cst}: {_err(cpl)}")

    # ---- actions + process steps (opt-in; EXECUTES upstream ops) -------------
    # Keyed by action/command key → "pass"/"fail" (matches the shared verified.json
    # contract read by verification.action_status / step_status). Notes are stored
    # alongside so a red cell explains itself; readers ignore the *Notes maps.
    actions_res: dict[str, str] = {}
    actions_notes: dict[str, str] = {}
    steps_res: dict[str, str] = {}
    steps_notes: dict[str, str] = {}
    act_counts = [0, 0]
    if _PROBE_ACTIONS:
        sample_id = str(sample.get("id") or "")
        meta = adapter.metadata()
        targets: list[tuple[str, dict[str, str], dict[str, str]]] = [
            (a["key"], actions_res, actions_notes)
            for a in (meta.get("actions") or [])
            if isinstance(a, dict) and a.get("key")
        ]
        targets += [
            (c["key"], steps_res, steps_notes)
            for g in (meta.get("processSteps") or [])
            for c in (g.get("commands") or [])
            if isinstance(c, dict) and c.get("key")
        ]
        # addTag/removeTag get a REAL effect-checked roundtrip (fresh title +
        # cleanup) instead of the generic reachability probe — see
        # _probe_tag_actions. Falls back to the generic probe when the
        # roundtrip could not cover the key (e.g. removeTag after a failed add).
        tag_results: dict[str, tuple[str, str]] | None = None
        if _ACTIONS_ONLY:
            targets = [t for t in targets if t[0] in _ACTIONS_ONLY]
        for akey, res_map, note_map in targets:
            if akey in ("addTag", "removeTag") and sample_id:
                if tag_results is None:
                    tag_results = await _probe_tag_actions(adapter, sample_id, base_url, token)
                status, note = tag_results.get(akey) or await _probe_action(
                    adapter, akey, sample_id, base_url, token
                )
            elif akey == "downloadPdf" and sample_id:
                status, note = await _probe_download_pdf(adapter, sample_id, base_url, token)
            else:
                status, note = await _probe_action(adapter, akey, sample_id, base_url, token)
            if status is None:
                continue
            res_map[akey] = status
            if note:
                note_map[akey] = note[:300]
            act_counts[0 if status != "fail" else 1] += 1

    # ✓ proven, ~ accepted (the request worked, the effect was not assertable),
    # ✗ failed. Collapsing the first two is how the old summary made a run look
    # better than it was.
    def tally(facet: str) -> str:
        c = counts[facet]
        return f"{c[PROVEN]}✓/{c['accepted']}~/{c['fail']}✗"

    observed = sum(1 for f in fields.values() if f.get("read") == PROVEN)
    summary = (
        f"read {observed}/{len(fields)} paths observed in {len(seen_records)} records | "
        f"filter {tally('filter')} | sort {tally('sort')} | search {tally('search')} | "
        f"update {tally('update')} | create {tally('create')}"
    )
    if _PROBE_ACTIONS:
        summary += f" | actions/steps {act_counts[0]}✓/{act_counts[1]}✗"

    # When these numbers were taken. Without it the workbook can only show the
    # file's mtime, which after a fresh clone is the checkout time — and a manifest
    # assembled from many scoped runs has no single age anyway: the seven sales
    # documents were re-probed weeks after the rest.
    result: dict[str, Any] = {"probedAt": _stamp(), "fields": fields}
    if actions_res or steps_res:
        result["actionsProbedAt"] = _stamp()
    if actions_res:
        result["actions"] = actions_res
        if actions_notes:
            result["actionsNotes"] = actions_notes
    if steps_res:
        result["processSteps"] = steps_res
        if steps_notes:
            result["processStepsNotes"] = steps_notes
    return result, summary


async def _main() -> None:
    base_url, token = _auth()
    # LIVE fixtures: a product id for line items, an existing catalogue tag for
    # tag roundtrips (an existing tag keeps the tag catalogue unpolluted).
    product_id: str | None = None
    supplier_id: str | None = None
    warehouse_id: str | None = None
    tag_title: str | None = None
    for adapter in CORE.adapters:
        if adapter.manifest.key == "Product" and product_id is None:
            p = _Probe(adapter, base_url, token)
            st, pl = await p.req(query=[("page[size]", "3")])
            for row in (pl.get("data") or []) if st == 200 else []:
                pid = str(row.get("id") or "")
                if pid:
                    product_id = pid.split("_", 1)[1] if "_" in pid else pid
                    break
        if adapter.manifest.key == "Supplier" and supplier_id is None:
            p = _Probe(adapter, base_url, token)
            st, pl = await p.req(query=[("page[size]", "3")])
            for row in (pl.get("data") or []) if st == 200 else []:
                sid = str(row.get("id") or "")
                if sid:
                    supplier_id = sid.split("_", 1)[1] if "_" in sid else sid
                    break
        # A storage location cannot be created without one — upstream has no
        # standalone endpoint, the location is created underneath its warehouse.
        if adapter.manifest.key == "Warehouse" and warehouse_id is None:
            p = _Probe(adapter, base_url, token)
            st, pl = await p.req(query=[("page[size]", "3")])
            for row in (pl.get("data") or []) if st == 200 else []:
                wid = str(row.get("id") or "")
                if wid:
                    warehouse_id = wid.split("_", 1)[1] if "_" in wid else wid
                    break
        if adapter.manifest.key == "Tag" and tag_title is None:
            p = _Probe(adapter, base_url, token)
            st, pl = await p.req(query=[("page[size]", "3")])
            for row in (pl.get("data") or []) if st == 200 else []:
                if row.get("label"):
                    tag_title = str(row["label"])
                    break
    path = os.path.join(os.path.dirname(__file__), "..", "verified.json")
    # A scoped run (VERIFY_ONLY) MERGES into the existing manifest so it never
    # wipes entities it didn't re-probe; a full run overwrites as before.
    previous: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: ASYNC230 - one-shot generator
            previous = json.load(fh).get("entities") or {}
    except (FileNotFoundError, ValueError):
        previous = {}
    entities: dict[str, Any] = dict(previous) if _ONLY else {}
    print(
        f"probing {'only ' + ', '.join(sorted(_ONLY)) if _ONLY else 'all entities'} "
        f"| actions/steps: {'ON' if _PROBE_ACTIONS else 'off'}"
    )
    for adapter in CORE.adapters:
        key = adapter.manifest.key
        if _ONLY and key not in _ONLY:
            continue
        try:
            result, summary = await _verify_entity(
                adapter, base_url, token, product_id, tag_title, supplier_id, warehouse_id
            )
        except _AuthFailed:
            # Deliberately NOT swallowed like a crashed probe: everything after this
            # point would be measuring our own expired token, and writing that down
            # is worse than writing nothing.
            raise
        except Exception as exc:  # noqa: BLE001 - one entity must not kill the run
            print(f"  {key}: probe crashed: {exc}")
            continue
        print(f"  {key}: {summary}")
        if not result and key in previous:
            # Unreadable today (a 404/405/422 on the test instance, or a hiccup) —
            # keep what an earlier run measured instead of deleting it. The block
            # carries its own probedAt, so the workbook shows its true age rather
            # than passing it off as fresh.
            entities[key] = previous[key]
            print(
                f"  {key}: kept the previous result (probed {previous[key].get('probedAt', '?')})"
            )
        if result:
            # A run that did not probe actions/steps must not delete the verdicts a
            # run that did left behind. Re-probing Quote for a field fix silently
            # dropped its send/addTag/removeTag results, and the sheet then read
            # "declared, untested" for capabilities that had been proven.
            # Carry forward every action/step verdict this run did not probe.
            # A run without VERIFY_ACTIONS probes none of them, and a run narrowed
            # by VERIFY_ACTIONS_ONLY probes one — in both cases writing only what
            # was measured would delete the rest. Measured while building the
            # narrow filter: SalesOrder went from six verdicts to one.
            before = previous.get(key) or {}
            if "actionsProbedAt" not in result and "actionsProbedAt" in before:
                result["actionsProbedAt"] = before["actionsProbedAt"]
            for res_key, note_key in (
                ("actions", "actionsNotes"),
                ("processSteps", "processStepsNotes"),
            ):
                kept = before.get(res_key) or {}
                if not kept:
                    continue
                fresh = result.get(res_key) or {}
                result[res_key] = {**kept, **fresh}
                # A note belongs to the verdict it explains: keep the old note only
                # where the old verdict survived.
                old_notes = {
                    k: v for k, v in (before.get(note_key) or {}).items() if k not in fresh
                }
                merged_notes = {**old_notes, **(result.get(note_key) or {})}
                if merged_notes:
                    result[note_key] = merged_notes
            entities[key] = result
    out = {
        "generatedAt": _stamp(),
        "instance": _INSTANCE,
        "schemaVersion": 2,
        "entities": entities,
    }
    with open(path, "w", encoding="utf-8") as fh:  # noqa: ASYNC230 - one-shot generator
        json.dump(out, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {os.path.abspath(path)}")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except _AuthFailed as exc:
        # Nothing is written. A partial file stamped with a fresh generatedAt would
        # claim the run happened, and its verdicts would say "broken" where the
        # truth is "not logged in".
        print(f"\nABORTED — not authenticated: {exc}")
        print("verified.json left untouched. Check XENTRAL_API_KEY / the Auth0 token.")
        raise SystemExit(2) from exc
