# AgentOS Neo (Xentral) — what lives in this folder

A core is the business-entity layer an ERP expert reviews and developers build
against. This file says which file holds which kind of truth, so nobody has to
read Python to find out whether something is a requirement, a result, or a guess.

**The one rule:** the specification and the test result are written by different
sides and never derived from one another. The moment one is generated from the
other, nothing is being checked any more.

## Owned by the ERP expert

| File | What it says |
|---|---|
| `erp-spec.yaml` | **What this core IS.** Per entity: every field with its type, whether a create must carry it, which operations it supports (`ops`), which of those a live run proved (`proven`), the status vocabulary — plus the executable capabilities with their evidence, and the ones the upstream cannot do with the measured reason. |
| `backlog.yaml` | **The parked work list.** 199 recorded field gaps with their business reason. Nothing loads it — it is a document, not a mechanism. |

The specification **describes**; it states no requirements. That is a deliberate
retreat: the file used to carry an inherited wish list, and the first spec-driven probe
run showed what that was worth — gaps claiming search does not work on fields where it
demonstrably does, never once tried. Requirements come back one at a time, from a human
who decided them, and `reviewed` records when that happened.

```yaml
number:   {type: string, ops: [create, filter, sort, search], proven: [filter, sort, search]}
customer: {type: reference, ref: Customer, required: true, ops: [create, filter], proven: [create, filter]}
```

`ops` is what the core offers. `proven` is what was measured. **They are not the same
claim**, and the difference is the point — a declared flag is not evidence.

The backlog is a hypothesis list, not a truth: eight of its entries are already marked
`disproven`, with the date a live run refuted them.

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

Measured when this README was written: of 80 capabilities the spec says the system
`can` do, **25 are proven** — 34 are only `reachable`, 3 `executed`, 18 never tested.
Each carries its verdict in the spec next to the capability, and CI checks it against
`verified.json` by value in both directions: a capability that gains a proof has to be
upgraded by hand, one that loses it downgraded. The count cannot drift upward quietly.

## The review sheet

`review.xlsx` is what a domain expert actually reads. It joins both sources
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
| `order.py` | The reading sequence the spec and the review sheet share. |
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
