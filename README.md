# Xentral Agent Cores

**Private** source of truth for the swappable **entity cores** of the Agent OS
backend (`xentral/agent-hub-labs`). A core is the definition of the business-entity
set (customers, products, documents, …) that every entity surface — workflows,
dashboards, Studio, tables, MCP tools — runs against.

Unlike [`xentral/agent-library`](https://github.com/xentral/agent-library) (which
ships **data** — JSON catalogues + Markdown skills), this repo ships **executable
Python**: each core is a package of adapters (`map_read`/`map_write`, upstream
proxying, status maps, actions). It is therefore **private** and gated by CI.

## How it reaches production

```
edit a core here → PR → merge → auto-tag (YYYY.MM.N)
        │
        ▼  vendored, pinned, at build time (no runtime fetch)
xentral/agent-hub-labs:  make bump-cores  →  backend/_vendor/cores/  →  rebuild/deploy
```

The backend pins an explicit `CORES_VERSION` and downloads that tag's tarball, so a
core change only reaches staging/prod once a **new tag** exists and the backend is
bumped. `backend/_vendor/cores/` is generated — **never edited directly**.

## The contract (why this repo depends on the backend)

Cores import a small, frozen contract from the backend package
(`entity_registry.core_sdk`): `CoreManifest`, `NativePolicy`, `EmulationManifest`,
`AdapterResponse`, the facade base, and helpers. Each core declares the
`CORE_CONTRACT_VERSION` it was built against; the backend loader rejects a core
built against an incompatible contract. See `CONTRIBUTING.md`.

## Layout

```
manifest.json            version + contractVersion + core index + checksums
cores/<id>/              one package per core, each exporting `CORE`
scripts/validate_cores.py   structure + contract-conformance + import-smoke
scripts/export_verified_xlsx.py  schema × verified.json → verified.xlsx
.github/workflows/       validate (PR) + release (auto-tag on merge)
```

### Capability workbook

`cores/<id>/verified.json` records only what a live run *proved*, so on its own it
cannot show what was never tested. `scripts/export_verified_xlsx.py` joins it with
the core's own schema into `cores/<id>/verified.xlsx` — one tab per entity listing
every field against read / create / update / filter / sort / search, plus all
actions and process steps. It reads only checked-in files, no tenant access:

```bash
PYTHONPATH=<agent-os>/backend \
  uv run --project <agent-os>/backend --with openpyxl \
  python scripts/export_verified_xlsx.py [core_id]
```

`offen` (declared but unproven) and `–` (not applicable per the schema) are both
*absent* from the JSON and mean opposite things — telling them apart is the point
of the sheet. Regenerate it after every `checks/verify.py` run.

A cell says how strongly the capability was shown, not merely that a request
succeeded. `cores/agentos_neo_xentral/verdicts.py` defines the words:

| cell | meaning |
| --- | --- |
| `ok` | the capability itself was demonstrated — a value observed, a write read back, an action's effect seen |
| `schwach` | probed, but the claim could not be asserted: HTTP 200 with nothing checked beyond the status, a declared field no record carried a value for, an action that ran without its effect being read back |
| `FEHLER` | probed and failed; the note says why |
| `offen` | declared, but no run has looked at it |
| `–` | not applicable per the schema |
| `Wunsch` | deliberately not possible (`priorities.json`), with a reason |

`schwach` is not automatically a to-do. For some entries it is the final answer:
`send` mails a real customer, so its effect cannot be verified on a live tenant.

## Local development

The backend's `make sync-cores` downloads a pinned tag into
`backend/_vendor/cores/` (private repo → authenticated). See the backend's
`CONTRIBUTING.md` and `make` targets.
