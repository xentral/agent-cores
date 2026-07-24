# AgentOS Neo (weclapp) — concept & gap analysis

> **Status:** Concept + Phase-0 scaffold (2026-07-24). Companion to the contract
> spec in the platform's `docs/concepts/entity-cores.md` and the authoring guide
> `docs/guides/building-an-erp-core.md`. This document says *whether* a weclapp
> core is feasible, *how* it is shaped, and *what is missing* before it is real.

## Summary

`agentos_neo_weclapp` is feasible and fits the established `agentos_neo_<backend>`
naming (alongside `agentos_neo_xentral`, `agentos_neo_postgres`). It is a
**Mode-C2 core** — a live passthrough over an external backend that speaks its
own protocol, so the adapter *translates at the boundary*. The reference to copy
is therefore a **combination**:

- **`odoo_core`** for the external-backend plumbing (per-tenant credentials,
  connect dialog, TTL cache, error contract, request dispatch) — this transfers
  almost verbatim.
- **`agentos_neo_xentral`** for the entity-modelling style (rich, curated
  `fields()` / `map_read()` / `map_write()` / `actions()` / `steps()` adapters).
- **A new weclapp REST translation layer** — the part that is genuinely new.

### The one structural difference from Odoo

`odoo_core` is **fully dynamic**: it discovers the entity roster from `ir.model`,
the field schema from `fields_get`, and write permissions from
`check_access_rights` — all live, per tenant. **weclapp has no equivalent
runtime schema-introspection API.** Its entity set and fields are fixed by the
product. So a weclapp core **cannot** be dynamic the Odoo way. The schema must
come from one of:

- **(a)** a build-time generator over weclapp's **OpenAPI spec** (`wEntityProperties`
  → `Entity` declarations), regenerable per weclapp version — the scaling path; or
- **(b)** **curated hand-modelled** entities (like `agentos_neo_xentral`'s ~22) — the
  fast path to a demoable, well-shaped slice.

Recommendation: start **(b)** for the flagship entities, keep **(a)** as the path
to full coverage (~150+ entities).

## What transfers unchanged (the scaffold already exists)

| Building block | Source | Effort |
|---|---|---|
| `CoreManifest` / `EmulatedOnly` / `adapters_factory` wiring | `cores/base.py` | trivial |
| Per-tenant credentials: `register_core_fields`, `resolve_core_credentials`, `CoreCredentialsMissing`, encrypted `integration_accounts`, `license_id` scoping | `entity_registry.cores.credentials` | trivial |
| Connect/manage lifecycle + catalog card (`kind: custom`, `auth_scheme: api_key`) | `ErpCoreConnectionProvider`, `integrations/providers/` | small |
| "Credentials missing" contract: 424 + `error_payload` + FE hint opening the connect dialog | `odoo_core/emulated/base.py` | trivial |
| `AdapterResponse` envelope incl. **both** `meta.total` and `extra.total` | odoo_core | trivial |
| `metadata()` → `rootNode.properties` render contract, prop vocabulary, sections | contract | medium (only populate) |
| TTL-cache-per-tenant, "stale beats broken" fallback | odoo_core | small |
| Status contract 424/502/405/404/400 + `_unreachable_metadata` | odoo_core | small |
| **Frontend: zero work** — selector rail, entity tiles, record counts, switch are generic | `entity_registry/routes.py` | — |
| Consumers (workflows, dashboards, MCP `xentral_entities`, Studio, `xentral_actions`) pick it up via the registry | `service.py`, `gateway.py` | — |

## What is new (the weclapp layer)

1. **Transport — REST, not JSON-RPC.** Per-entity routes
   `GET/POST/PUT/DELETE /webapp/api/v1/<entity>` and `.../<entity>/id/{id}`
   (note the literal `id/` path segment). Replaces Odoo's single `execute_kw`
   endpoint entirely.
2. **Query translation.** Our `filter[i][key|op|value]` / `sort` / `page[…]` →
   weclapp querystring: operator suffixes `?field-eq=`, `-ne`, `-gt/-lt/-ge/-le`,
   `-like/-ilike`, `-in/-notin`, `-null/-notnull`; `sort=field` / `-field`;
   `page` + `pageSize`; field selection via `properties`/`additionalProperties`;
   count via `GET /<entity>/count`.
3. **Schema source.** From OpenAPI (build-time) or hand-modelled — see the
   structural difference above.
4. **Data-type translation in the record-transform seam:**
   - dates/times as **Unix epoch milliseconds** ↔ our `date`/`datetime`
   - references as `<entity>Id` **string FKs** (`customerId`, `articleId`) ↔ our
     `reference {id, renderProperty}` (resolve related via a follow-up filtered GET)
   - embedded sub-objects (`orderItems`, `recordAddress`) ↔ `collection`/`embedded`
   - enums as string constants (`leadStatus`, `partyType`) ↔ `select` options
   - boolean filter values as the strings `"true"`/`"false"`
5. **Auth + probe.** Static header `AuthenticationToken: <token>` (no uid
   handshake — simpler than Odoo). Connect fields: `weclapp_base_url` +
   `weclapp_api_token` (secret). Probe = authenticated `GET /system/permissions`.

## Gap analysis — what is missing / risks

| # | Gap | Severity | Handling |
|---|---|---|---|
| **G1** | **No runtime schema introspection.** Not dynamically discoverable like Odoo. | **High** (core design decision) | OpenAPI generator or curated hand-model. Start curated, generator as scaling path. |
| **G2** | **Custom attributes (tenant fields)** are not in the OpenAPI spec; they arrive as a `customAttributes` collection only in live data. | Medium | Optional discovery step: merge `customAttributes` from live records / definitions into the schema. Phase 2. |
| **G3** | **Party polymorphism.** No `/customer` or `/supplier` route — everything is `/party`, discriminated by role flags (`customer=true`, `supplier=true`, `partyType`, `leadStatus`). | Medium | A logical entity (Customer) = `/party` + a fixed filter. Odoo's `base_domain` + `create_defaults` pattern transfers directly. |
| **G4** | **No PATCH — PUT is full-object replacement.** Partial update only via `ignoreMissingProperties`. Our update contract is partial. | Medium | Read-modify-write, or set `ignoreMissingProperties`, carefully — otherwise fields are clobbered/nulled. Odoo's partial `write` does not help here. |
| **G5** | **Rate limits (429) undocumented.** Public ceiling unknown. | Medium | 429 backoff/retry (odoo_core has none). Measure against a live tenant. |
| **G6** | **Uneven CRUD verbs.** Many entities are read-only / update-only (no POST/PUT/DELETE in Swagger). | Small | Derive `operations` per entity from the spec (NoCreate/NoUpdate/NoDelete). |
| **G7** | **Sub-entities require a parent scope** (e.g. `comment` needs entity+id filter). | Small | Model as a collection on the parent, or require the scoping filter. |
| **G8** | **No generic bulk-write.** Only client-side `multiRequest` read bundling; bulk data via CSV/XML/EDI. | Small | Irrelevant for the single-record entity gateway. |

**Opportunity, not a gap:** unlike `odoo_core` (which deliberately omits both),
weclapp supports **DELETE** and rich **action endpoints** (`createShipment`,
`createSalesInvoice`, `quotation/id/{id}/accept`, …). The actions/process-steps
abstraction already exists in `agentos_neo_xentral` (`action_map`, `steps()`,
`actions()` + the three routing families), so a weclapp core can be *richer* than
the Odoo one — at the cost of a routing layer. `GET /system/permissions` can also
gate create/update per token (the analogue of Odoo's `check_access_rights`).

## Phased plan

- **Phase 0 — scaffold (this commit):** manifest (`EmulatedOnly`,
  `adapters_factory`), credential fields + `register_core_fields`, REST client
  foundation with `AuthenticationToken`, connection provider + catalog card +
  probe. Core registers, appears in the rail, connect flow validates the token —
  **no entities yet** (empty roster).
- **Phase 1 — flagship read slice (in progress):** the adapter engine
  (`WeclappAdapterBase`) + curated entities, one at a time. **Done: Customer via
  `/party`** (list/read, filter/sort/pagination/counts, epoch-ms + FK + collection
  translation, credentials-missing 424) with engine unit tests. **Next:** Article,
  SalesOrder, SalesInvoice, Shipment.
- **Phase 2 — write + actions:** create/update (PUT / `ignoreMissingProperties`),
  delete, key action routes, `customAttributes` merge.
- **Phase 3 — scale:** OpenAPI generator for the remaining ~150 entities; verify
  live via the tool suite.

## Verify against a live tenant before building further

Two weclapp facts came from generated SDKs, not weclapp's own prose:

1. The **complete operator-suffix list** (`-gt/-lt/-like/-ilike/…`) — confirm
   against the tenant's Swagger at `https://<tenant>.weclapp.com/webapp/api/v1/`.
2. The **concrete 429 rate-limit ceiling** — measure.

## File map (two repos)

```
xentral/agent-cores  (this repo — the core package, vendored into the backend)
  cores/agentos_neo_weclapp/
    __init__.py                 exports CORE + CONTRACT_VERSION
    manifest.py                 CORE = CoreManifest(id="agentos_neo_weclapp", EmulatedOnly, adapters_factory)
    emulated/__init__.py        exports build_adapters
    emulated/base.py            WECLAPP_FIELDS + register_core_fields + WeclappClient +
                                declaration types + pure translation helpers + WeclappAdapterBase
    emulated/entities.py        curated Entity declarations + build_adapters() (Customer; more to come)
    docs/00-concept.md          this document
    # + tests/test_agentos_neo_weclapp.py in the repo root's tests/

agent-hub-labs  (the platform repo — connection + catalog only)
  backend/integrations/providers/agentos_neo_weclapp/provider.py   WeclappConnectionProvider (+ probe)
  backend/tools_registry/registry.py                               register the provider
  backend/integrations/catalog.py                                  ERP-core card + _CUSTOM_ERP_CORE_IDS
```

Note: the backend discovers cores from `backend/_vendor/cores` (vendored via
`CORES_VERSION`) or from `XENTRAL_CORES_ROOT`. For local dev point
`XENTRAL_CORES_ROOT` at this repo's `cores/`; to ship, cut an agent-cores tag and
`make bump-cores`.
