# Contributing — Xentral Agent Cores

This **private** repo is the single source of truth for the entity **cores** the
Agent OS backend vendors at build time (pinned tag — no runtime fetch). Cores are
**executable Python that runs server-side with a tenant's Xentral credentials**, so
the bar is higher than for the (data-only) `agent-library`: every change is CI-gated
and only signed/reviewed tags reach a release.

## Layout

```
manifest.json            version + contractVersion + core index + checksums
cores/
  <id>/                  one importable package per core, exporting `CORE`
    __init__.py          `from .manifest import CORE`  (+ `CONTRACT_VERSION = N`)
    manifest.py          the CoreManifest (id, label, native policy, adapters)
    emulated/            adapters (map_read/map_write, upstream proxying)
    …
scripts/validate_cores.py
.github/workflows/{validate,release}.yml
```

## The contract

Cores import ONLY from the backend's frozen `entity_registry.core_sdk`
(`CoreManifest`, `NativePolicy`, `EmulationManifest`, `AdapterResponse`, the facade
base, helpers). A core must NOT import another core, and must NOT import backend
internals outside `core_sdk`.

Each core declares the contract version it targets (`CONTRACT_VERSION` in its
`__init__.py`), mirrored in `manifest.json` → `contractVersion`. The backend loader
rejects a core whose contract version it does not support. Bump it only when the
backend's `core_sdk` changes in a breaking way (coordinated with agent-hub-labs).

## Conventions

- **One package per core** under `cores/<id>/`, `id` matching the `CoreManifest.id`.
- **No secrets, no tenant data.** CI secret-scans every PR.
- **CI gates**: JSON well-formed · `scripts/validate_cores.py` (structure +
  contract-conformance) · import-smoke against the pinned backend contract · secret
  scan · read-only live smoke (selfcheck) against the test tenant.
- **Releases**: `main` is review-protected; merging auto-cuts a `YYYY.MM.N` tag. The
  backend picks it up via `make bump-cores`.

## Local check

```bash
python scripts/validate_cores.py
```
