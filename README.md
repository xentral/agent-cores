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
.github/workflows/       validate (PR) + release (auto-tag on merge)
```

## Local development

The backend's `make sync-cores` downloads a pinned tag into
`backend/_vendor/cores/` (private repo → authenticated). See the backend's
`CONTRIBUTING.md` and `make` targets.
