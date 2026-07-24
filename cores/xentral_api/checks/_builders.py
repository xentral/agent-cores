"""Shared building blocks for the per-entity document checks (xentral_api).

Each entity file (``sales_order.py`` …) declares a small CONFIG and calls
``build_document_checks(CONFIG)`` to get the standard set:

    read_all · fields · address · writeprotection · tags

plus an optional ``lifecycle_check(CONFIG)`` (destructive) where the entity has
status transitions worth exercising on a throwaway record.

CONFIG shape::

    {"slug": "sales_order", "entity": "SalesOrder", "fixture": "sales_order_id",
     "fields": [("internalComment", STR), ("fastLane", BOOL), ...]}

All writes are reversible (capture → write test value → verify → restore); the
write-protection and tag checks are net-zero toggles. Everything goes through
the emulated business-entity gateway — the same path Studio and workflows use.
Results are recorded into ``ctx.verified`` (the per-capability manifest).
"""

from __future__ import annotations

import json
from typing import Any

from tests.tool_suite.harness import Check, Ctx

CAT = "documents"

# Field kinds for the curated write-roundtrip: booleans toggle, strings get a
# marker appended (then restored). Marker is what a string roundtrip writes.
STR = "str"
BOOL = "bool"
_MARK = " [tool-suite]"


# ── gateway plumbing ──────────────────────────────────────────────────────
async def _auth(ctx: Ctx) -> tuple[str | None, str | None]:
    """(base_url, token) for the target instance. Resolves the instance URL via
    the Instance-Manager directory (which also primes the shared URL cache) —
    the headless equivalent of what the running backend does at startup — then
    fetches a client-credentials Xentral token. Cached on ctx for the run."""
    cached = getattr(ctx, "_doc_auth", None)
    if cached is not None:
        return cached
    from auth.token_manager import AuthTokenManager
    from config.instance_manager_client import InstanceManagerClient

    email = ctx.fixture("owner_email") or ctx.fixture("test_email")
    base_url = token = None
    try:
        if email:
            insts = InstanceManagerClient().get_user_instances(email)
            match = next((i for i in insts if str(i.license_id) == str(ctx.instance_id)), None)
            if match and match.instance_url:
                base_url = match.instance_url.rstrip("/")
        token = AuthTokenManager().get_xentral_token_sync(ctx.instance_id)
    except Exception:  # noqa: BLE001, S110 — surfaced as an auth-less result the checks report
        pass
    ctx._doc_auth = (base_url, token)  # type: ignore[attr-defined]
    return base_url, token


async def _gw(
    ctx: Ctx,
    entity: str,
    method: str,
    *,
    handle: str | None = None,
    query: list[tuple[str, str]] | None = None,
    body: Any = None,
    action: str | None = None,
) -> tuple[int, Any]:
    """One emulated-gateway call (CRUD via handle_emulated_request, or an entity
    action via handle_emulated_action), recorded in ctx.calls for the report."""
    from entity_registry.core_sdk import resolve_active_core
    from entity_registry.emulated.gateway import (
        handle_emulated_action,
        handle_emulated_request,
    )

    base_url, token = await _auth(ctx)
    if not base_url or not token:
        return (0, {"error": "no reachable instance / token"})
    core = await resolve_active_core(ctx.instance_id)
    adapters = core.resolve_adapters()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if action is not None:
        envelope = json.dumps({"ids": [handle], "command": body or {}}).encode("utf-8")
        resp = await handle_emulated_action(
            entity_key=entity,
            action_key=action,
            body=envelope,
            base_url=base_url,
            token=token,
            adapters=adapters,
        )
    else:
        resp = await handle_emulated_request(
            entity_key=entity,
            method=method,
            base_url=base_url,
            token=token,
            handle=handle,
            query=query or [],
            body=payload,
            adapters=adapters,
        )
    status = getattr(resp, "status_code", 0) if resp is not None else 0
    parsed: Any = None
    content = getattr(resp, "content", None)
    if content:
        try:
            parsed = json.loads(content.decode("utf-8"))
        except Exception:  # noqa: BLE001 — a non-JSON body is still reportable
            parsed = content.decode("utf-8", "replace")[:200]
    label = action or f"{method} {handle or ''}".strip()
    ctx.calls.append(
        {
            "tool": f"gateway:{entity}.{label}",
            "args": {k: v for (k, v) in (query or [])} if query else (body or {}),
            "error": status == 0 or status >= 400,
            "response": (json.dumps(parsed, ensure_ascii=False, default=str) or "")[:600],
        }
    )
    return status, parsed


def _record(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict):
        data = parsed.get("data", parsed)
        return data if isinstance(data, dict) else {}
    return {}


async def _read_one(ctx: Ctx, entity: str, handle: str) -> dict[str, Any]:
    _, parsed = await _gw(ctx, entity, "GET", handle=handle)
    return _record(parsed)


async def _resolve_id(ctx: Ctx, entity: str, fixture_key: str | None) -> str | None:
    """A record id: the fixture if given, else the first from a bare list."""
    if fixture_key:
        fx = ctx.fixture(fixture_key)
        if fx:
            return str(fx)
    _, parsed = await _gw(ctx, entity, "GET", query=[("page[size]", "1")])
    rows = parsed.get("data") if isinstance(parsed, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        rid = rows[0].get("id")
        return str(rid) if rid is not None else None
    return None


async def _entity_schema(ctx: Ctx, entity: str) -> dict[str, Any] | None:
    """The active core's render schema for an entity (its metadata())."""
    from entity_registry.core_sdk import resolve_active_core
    from entity_registry.core_sdk import resolve_emulated_in

    core = await resolve_active_core(ctx.instance_id)
    adapter = resolve_emulated_in(entity, core.resolve_adapters())
    return adapter.metadata("en") if adapter is not None else None


def _writable_scalar_fields(schema: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Every root field the schema declares as writable AND reversibly
    roundtrippable — i.e. plain ``string``/``boolean`` fields that are not
    ``access:"readOnly"``. References, selects, dates, numbers, embedded and
    collections are skipped (the roundtrip can't safely fuzz those)."""
    props = ((schema or {}).get("rootNode") or {}).get("properties") or {}
    out: list[tuple[str, str]] = []
    for name, spec in props.items():
        if not isinstance(spec, dict) or spec.get("access") == "readOnly":
            continue
        if spec.get("type") == "string":
            out.append((name, STR))
        elif spec.get("type") == "boolean":
            out.append((name, BOOL))
    return out


# ── verified-manifest accumulators ────────────────────────────────────────
def _mark_field(ctx: Ctx, entity: str, field: str, kind: str, status: str) -> None:
    ctx.verified.setdefault(entity, {}).setdefault("fields", {}).setdefault(field, {})[kind] = (
        status
    )


def _mark_group(ctx: Ctx, entity: str, group: str, key: str, status: str) -> None:
    """group ∈ {operations, actions, processSteps}."""
    ctx.verified.setdefault(entity, {}).setdefault(group, {})[key] = status


# ── check builders ────────────────────────────────────────────────────────
def _read_all_check(cfg: dict) -> Check:
    slug, entity = cfg["slug"], cfg["entity"]

    async def fn(ctx: Ctx):
        rid = await _resolve_id(ctx, entity, cfg.get("fixture"))
        if not rid:
            return (None, f"no {entity} record on tenant")
        status, parsed = await _gw(ctx, entity, "GET", handle=rid)
        if status != 200:
            return (False, f"read {entity} {rid}: HTTP {status}")
        rec = _record(parsed)
        _mark_group(ctx, entity, "operations", "read", "pass")
        for k, v in rec.items():
            _mark_field(ctx, entity, k, "read", "pass")
            if k == "documentAddress" and isinstance(v, dict):
                for sub in v:
                    _mark_field(ctx, entity, f"documentAddress.{sub}", "read", "pass")
        populated = sum(1 for v in rec.values() if v not in (None, "", [], {}))
        return (True, f"{entity} {rid}: read ok, {populated}/{len(rec)} fields populated")

    return Check(name=f"doc:{slug}.read_all", category=CAT, fn=fn, kind="read")


def _field_roundtrip_check(cfg: dict) -> Check:
    slug, entity = cfg["slug"], cfg["entity"]

    async def fn(ctx: Ctx):
        rid = await _resolve_id(ctx, entity, cfg.get("fixture"))
        if not rid:
            return (None, f"no {entity} record on tenant")
        # The curated list, optionally UNIONed with every writable scalar field
        # derived from the schema (``derive_fields``) so the write coverage is
        # the entity's full writable surface, not a hand-picked sample.
        fields = list(cfg.get("fields", []))
        if cfg.get("derive_fields"):
            have = {name for name, _ in fields}
            derived = _writable_scalar_fields(await _entity_schema(ctx, entity))
            fields += [(name, kind) for name, kind in derived if name not in have]
        rec = await _read_one(ctx, entity, rid)
        if not rec:
            return (False, f"could not read {entity} {rid}")
        protected = bool(rec.get("isWriteProtected"))
        if protected:
            await _gw(ctx, entity, "PATCH", handle=rid, action="removeWriteProtection")

        ok: list[str] = []
        bad: list[str] = []
        try:
            for field, kind in fields:
                if field not in rec:
                    continue
                original = rec.get(field)
                if kind == BOOL:
                    test = not bool(original)
                else:
                    base = original if isinstance(original, str) else ""
                    test = (base + _MARK) if _MARK not in base else base.replace(_MARK, "")
                st, _ = await _gw(ctx, entity, "PATCH", handle=rid, body={field: test})
                if st >= 400:
                    bad.append(f"{field}:set{st}")
                    _mark_field(ctx, entity, field, "write", "fail")
                    continue
                after = (await _read_one(ctx, entity, rid)).get(field)
                changed = after == test or after != original
                await _gw(ctx, entity, "PATCH", handle=rid, body={field: original})
                _mark_field(ctx, entity, field, "write", "pass" if changed else "fail")
                (ok if changed else bad).append(field if changed else f"{field}:noop")
        finally:
            if protected:
                await _gw(ctx, entity, "PATCH", handle=rid, action="setWriteProtection")
        if ok:
            _mark_group(ctx, entity, "operations", "update", "pass")

        if bad:
            return (False, f"{entity} {rid}: ok={ok} | issues={bad}")
        if not ok:
            return (None, f"{entity} {rid}: none of the curated fields present")
        return (True, f"{entity} {rid}: {len(ok)} fields set+restored ({', '.join(ok)})")

    return Check(name=f"doc:{slug}.fields", category=CAT, fn=fn, kind="write")


def _address_roundtrip_check(cfg: dict) -> Check:
    """Change the recipient name + street and restore — net-zero."""
    slug, entity = cfg["slug"], cfg["entity"]

    async def fn(ctx: Ctx):
        rid = await _resolve_id(ctx, entity, cfg.get("fixture"))
        if not rid:
            return (None, f"no {entity} record on tenant")
        rec = await _read_one(ctx, entity, rid)
        addr = rec.get("documentAddress")
        if not isinstance(addr, dict):
            return (None, f"{entity} {rid}: no documentAddress")
        protected = bool(rec.get("isWriteProtected"))
        if protected:
            await _gw(ctx, entity, "PATCH", handle=rid, action="removeWriteProtection")
        try:
            changed = dict(addr)
            changed["name"] = str(addr.get("name") or "") + _MARK
            changed["street"] = str(addr.get("street") or "") + _MARK
            st, _ = await _gw(ctx, entity, "PATCH", handle=rid, body={"documentAddress": changed})
            if st >= 400:
                _mark_field(ctx, entity, "documentAddress.name", "write", "fail")
                _mark_field(ctx, entity, "documentAddress.street", "write", "fail")
                return (False, f"{entity} {rid}: address PATCH HTTP {st}")
            after = (await _read_one(ctx, entity, rid)).get("documentAddress") or {}
            took = after.get("name") == changed["name"] and after.get("street") == changed["street"]
            await _gw(ctx, entity, "PATCH", handle=rid, body={"documentAddress": addr})
            verdict = "pass" if took else "fail"
            _mark_field(ctx, entity, "documentAddress.name", "write", verdict)
            _mark_field(ctx, entity, "documentAddress.street", "write", verdict)
            if not took:
                return (False, f"{entity} {rid}: name/street did not take")
            return (True, f"{entity} {rid}: name+street set+restored")
        finally:
            if protected:
                await _gw(ctx, entity, "PATCH", handle=rid, action="setWriteProtection")

    return Check(name=f"doc:{slug}.address", category=CAT, fn=fn, kind="write")


def _writeprotection_roundtrip_check(cfg: dict) -> Check:
    slug, entity = cfg["slug"], cfg["entity"]

    async def fn(ctx: Ctx):
        rid = await _resolve_id(ctx, entity, cfg.get("fixture"))
        if not rid:
            return (None, f"no {entity} record on tenant")
        rec = await _read_one(ctx, entity, rid)
        was = bool(rec.get("isWriteProtected"))
        first = "removeWriteProtection" if was else "setWriteProtection"
        second = "setWriteProtection" if was else "removeWriteProtection"
        s1, _ = await _gw(ctx, entity, "PATCH", handle=rid, action=first)
        s2, _ = await _gw(ctx, entity, "PATCH", handle=rid, action=second)
        ok = s1 < 400 and s2 < 400
        verdict = "pass" if ok else "fail"
        _mark_group(ctx, entity, "processSteps", "setWriteProtection", verdict)
        _mark_group(ctx, entity, "processSteps", "removeWriteProtection", verdict)
        if not ok:
            return (False, f"{entity} {rid}: {first}={s1} {second}={s2}")
        return (True, f"{entity} {rid}: write-protection {first}->{second} ok (restored)")

    return Check(name=f"doc:{slug}.writeprotection", category=CAT, fn=fn, kind="write")


def _tag_roundtrip_check(cfg: dict) -> Check:
    slug, entity = cfg["slug"], cfg["entity"]

    async def fn(ctx: Ctx):
        rid = await _resolve_id(ctx, entity, cfg.get("fixture"))
        if not rid:
            return (None, f"no {entity} record on tenant")
        s1, _ = await _gw(
            ctx, entity, "PATCH", handle=rid, action="addTag", body={"title": "tool-suite-test"}
        )
        if s1 >= 400:
            _mark_group(ctx, entity, "actions", "addTag", "fail")
            return (False, f"{entity} {rid}: addTag HTTP {s1}")
        _mark_group(ctx, entity, "actions", "addTag", "pass")
        s2, _ = await _gw(
            ctx, entity, "PATCH", handle=rid, action="removeTag", body={"title": "tool-suite-test"}
        )
        _mark_group(ctx, entity, "actions", "removeTag", "fail" if s2 >= 400 else "pass")
        if s2 >= 400:
            return (False, f"{entity} {rid}: removeTag HTTP {s2} (tag may linger)")
        return (True, f"{entity} {rid}: tag add+remove ok (net zero)")

    return Check(name=f"doc:{slug}.tags", category=CAT, fn=fn, kind="write")


def lifecycle_check(cfg: dict) -> Check:
    """Release → cancel the record from fixtures. DESTRUCTIVE: changes real
    document state and is not cleanly reversible — only runs with --destructive.
    Add it (per entity) only where a throwaway record is configured."""
    slug, entity, fixture = cfg["slug"], cfg["entity"], cfg.get("fixture")

    async def fn(ctx: Ctx):
        rid = ctx.fixture(fixture) if fixture else None
        if not rid:
            return (None, f"needs fixture: {fixture} (a throwaway {entity})")
        rid = str(rid)
        rec = await _read_one(ctx, entity, rid)
        start = rec.get("documentStatus")
        s_rel, _ = await _gw(ctx, entity, "PATCH", handle=rid, action="release")
        s_can, _ = await _gw(ctx, entity, "PATCH", handle=rid, action="cancel")
        _mark_group(ctx, entity, "processSteps", "release", "fail" if s_rel >= 400 else "pass")
        _mark_group(ctx, entity, "processSteps", "cancel", "fail" if s_can >= 400 else "pass")
        if s_rel >= 400 and s_can >= 400:
            return (False, f"release={s_rel} cancel={s_can} (start status={start})")
        return (
            True,
            f"{entity} {rid}: release={s_rel} cancel={s_can} (start={start}; state changed, not restored)",
        )

    return Check(name=f"doc:{slug}.lifecycle", category=CAT, fn=fn, kind="write", destructive=True)


def build_document_checks(cfg: dict) -> list[Check]:
    """The standard reversible set for one document entity, in display order."""
    return [
        _read_all_check(cfg),
        _field_roundtrip_check(cfg),
        _address_roundtrip_check(cfg),
        _writeprotection_roundtrip_check(cfg),
        _tag_roundtrip_check(cfg),
    ]
