# AgentOS Neo (Xentral) — what lives in this folder

A core is the business-entity layer an ERP expert reviews and developers build
against. This file says which file holds which kind of truth, so nobody has to
read Python to find out whether something is a requirement, a result, or a guess.

**The one rule:** the specification and the test result are written by different
sides and never derived from one another. The moment one is generated from the
other, nothing is being checked any more.

## Owned by the ERP expert — the requirement

| File | What it says |
|---|---|
| `capabilities.spec.yaml` | What the ERP must be able to **do**: per entity, which actions and status steps must exist, which statuses, which fields are mandatory on create, what must be filterable — **and, for each capability the upstream cannot do, why not**. |
| `field-gaps.yaml` | What the ERP must be able to **record and find**: field × operation the merchant needs but the Xentral API cannot do today, each with the business reason ("a clerk must be able to set and correct the customer's PO number"). |

Two files, one specification, two axes: what must be *doable*, and what must be
*recordable and findable*. Both are hand-written. Neither is generated from the
code — a specification derived from the implementation cannot state what is still
missing, which is the only reason anyone would review it.

Both ship. The core loads them at runtime and renders each recorded gap where it
applies — on the field, or on the action — so a builder reads *"not possible, and
here is why"* at the point of use instead of mere absence. Absence is
indistinguishable from "nobody looked".

That is why the reasons live here and not in the code. They are business
statements — *"the transition happens only in the UI"*, *"digest-authenticated and
behind a killswitch"* — and until recently the 100 capability reasons sat in the
adapter Python, where the person who owns the requirement could not edit them. The
adapter now says only **that** something is a gap (`wish=True`); this file says
**why**.

The split is deliberate and load-bearing: if the specification decided *which*
capabilities are gaps as well as why, the two rules that matter most — a required
capability must not be a gap, a recorded gap must still be one — would be comparing
the specification against itself.

## Written by the machine — the result

| File | What it says |
|---|---|
| `verified.json` | What a live run against a real tenant actually demonstrated. Header records which instance and when. **Never edit by hand.** |
| `verified.xlsx` | Field × facet detail of the same data. Regenerate with `scripts/export_verified_xlsx.py`. |
| `review.yaml` / `review.xlsx` | **The review sheet** — specification, implementation and evidence on one row. Regenerate with `scripts/export_review_sheet.py`. |

`verified.json` grades how strongly each capability was shown, and the distinction
carries the weight:

| Verdict | Meaning |
|---|---|
| `pass` | The effect was read back. This is proof. |
| `executed` | The call changed something, nobody checked what. |
| `reachable` | The route exists and refused our probe. **Proves nothing either way.** |
| `fail` | Tested and broken. |
| *(absent)* | Never tested. |

Measured when this README was written: of 80 capabilities the spec calls
executable, **25 are proven**. The rest are listed by name in the spec's
`evidenceGaps` section, and CI keeps that list matching reality in both
directions — so the count cannot drift upward quietly, and a capability that gains
a real proof has to be struck from it by hand.

## The review sheet

`review.xlsx` is what a domain expert actually reads. It joins all three sources
onto one row, ordered along the process chain (customer → quote → order → delivery
→ invoice → return → credit note, then purchasing, then master data) rather than
alphabetically, so a document sits next to the one it produces.

| Sheet | One row per | Columns |
|---|---|---|
| Übersicht | entity | required · proven · unproven · accepted gaps · not built · field requirements · review status |
| Fähigkeiten | capability | Soll · Ist · Beweis · reason |
| Felder | field requirement | operations · status · business reason · hint |

`review.yaml` is the same data for tooling and for PR review. It carries **no
generation timestamp** — only the stamp of the probe run it was built from — so the
file changes when a fact changes, not when someone re-runs the export. A diff on it
means something moved.

Two things the sheet surfaces that neither source shows alone: a recorded field gap
whose flag the schema now declares (or that a live run has since proven) is flagged
*"offen — prüfen, evtl. erledigt"*, because a stale gap outranks a real capability
wherever it is shown; and a gap naming a field path the model does not have at all
is flagged outright.

Regenerate:

```bash
PYTHONPATH=<agent-os>/backend \
  uv run --project <agent-os>/backend --with openpyxl --with pyyaml \
  python scripts/export_review_sheet.py
```

## Built by developers

| File | Role |
|---|---|
| `emulated/*.py` | The implementation, one module per business object. |
| `manifest.py` | Which adapters this core exposes. |
| `checks/verify.py` | The prober: runs against a real tenant and writes `verified.json`. |
| `descriptions.json` | Field descriptions surfaced in `describe`. |
| `playbook.md` | Short prose for agents building workflows. CI reads the document itself: every dotted field path and every `Entity.name` it names in code spans must exist in the core. |
| `docs/` | Investigations and findings behind individual decisions. |

## The loop

1. The expert states in the **spec** what has to be possible.
2. Developers build it in **`emulated/`**.
3. **`checks/verify.py`** runs against a real tenant and writes **`verified.json`**.
4. **`scripts/export_review_sheet.py`** puts requirement, implementation and
   evidence side by side in `review.xlsx` / `review.yaml`.
5. The expert reads the sheet: still missing anything? Is a documented gap
   acceptable? Is an unproven capability good enough?
6. CI fails whenever spec and core drift apart — in *both* directions: something
   required but not built, and something built that no spec mentions.

Sign-off is recorded in the spec's `reviewed` section. It is empty today: nothing
in this core has been through a domain review yet, and the sheet has to be able to
say so rather than imply a check that never happened.

## Checks

```bash
python scripts/validate_cores.py
PYTHONPATH=<agent-os>/backend pytest tests/test_core_playbooks.py tests/test_review_sheet.py
```
