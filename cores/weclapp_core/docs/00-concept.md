# weclapp (native) — a generated 1:1 mirror core

> **Status:** WIP sketch + working generator (2026-07-25). Sibling to the curated
> `agentos_neo_weclapp` core. Where that one is a hand-curated **AgentOS Neo**
> shape over weclapp, this one is a **faithful mirror** of weclapp's own API —
> entity and field names verbatim — **generated from weclapp's OpenAPI spec**.

## Why a second core

Two philosophies, both valid, offered side by side in the selector:

| | `agentos_neo_weclapp` (curated) | `weclapp_core` (native, generated) |
|---|---|---|
| Entities | ~9 curated flagships | all schemas in the spec (~150+) |
| Party | split into Customer + Supplier | one polymorphic `party` |
| Fields/entity | ~10–15 curated | the full weclapp set (30–80+) |
| Names | lightly normalized (`documentNumber`, `party`) | verbatim (`salesOrderNumber`, `customerId`) |
| Source | hand-authored declarations | **generated from OpenAPI**, regenerable |
| Audience | agents / workflows (uniform with the Xentral core) | power users / customizing (matches weclapp docs 1:1) |

## The key lever: the engine already exists

The runtime (`WeclappAdapterBase` in `agentos_neo_weclapp/emulated/base.py`) —
transport (`AuthenticationToken`, `/webapp/api/v1`), query translation
(`filter/sort/page` → weclapp params), record transform (epoch-ms → ISO,
`<x>Id` → reference, `*Items` → collection), the envelope and the 424 contract —
is **reused as-is**. This core only supplies a **different, generated set of
`Entity` declarations**. It imports the engine from the curated core rather than
duplicating it (a later refactor can extract a shared `weclapp/engine` module).

## The generator (build time — weclapp has no runtime schema API)

```
weclapp OpenAPI spec  ──►  build_entities_from_openapi(spec)  ──►  tuple[Entity, …]
(openapi.json, per-tenant                    │                        │
 or the public spec)                         │                        └─► WeclappAdapterBase
                                             └─ falls back to openapi.sample.json
```

Per schema the generator derives:

- **scalars** from `properties` (`type`/`format`) → our render vocabulary
- **dates**: integer fields whose name looks date-like → `epoch=True` (weclapp
  serializes dates as Unix-epoch-milliseconds)
- **references**: `<name>Id` string FKs → `reference`; the target is the entity
  name, via a small alias table for weclapp's polymorphic party
  (`customerId`/`supplierId`/… → `party`)
- **enums** → `select` with options
- **collections**: array-of-object properties (e.g. `orderItems`) → `collection`
- **embeds**: nested object properties (e.g. `deliveryAddress`) → `embedded`
- **operations**: read-only (`list`/`read`) for v1

The spec is a **build-time artifact** (committed + versioned, regenerable per
weclapp API version) — exactly like the `shape.json` of the mirror cores.

## One connection, two cores

Credentials resolve under the connector id `agentos_neo_weclapp` (the shared
`WeclappClient` in the engine). So this core **reuses the same weclapp connection**
the curated core already uses — the tenant connects once (base URL + API token),
both cores work. No separate connect card or credential registration.

## Not done yet (needs a live tenant / key)

- **Point the generator at a real spec.** This package ships a small
  `openapi.sample.json` (SalesOrder, Party, Article) so the core loads a
  demonstrative set and the mapping is unit-tested. Drop the tenant's exported
  `openapi.json` next to it (it takes precedence) to generate the full mirror.
- **Write paths.** v1 is read-only. weclapp's spec marks `readOnly` properties and
  which entities expose POST/PUT/DELETE — a later phase derives `operations` and
  per-field `writable` from that, plus a 429 backoff.
- **customAttributes.** Per-tenant custom fields are not in the OpenAPI; they need
  a runtime merge (a sample record or the custom-attribute-definition endpoint).
- **Filter/sort capabilities** are currently marked optimistically on scalars;
  reconcile against the live API (weclapp 400s on a non-filterable property).
- **Roster hygiene.** 150+ entities want categories/an allowlist so the selector
  isn't a wall of names.

Marked `labs=True` + `read_only=True` until verified against a live tenant.
