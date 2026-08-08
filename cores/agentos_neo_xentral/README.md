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
| `capabilities.spec.yaml` | What the ERP must be able to **do**: per entity, which actions and status steps must exist, which statuses, which fields are mandatory on create, what must be filterable. |
| `priorities.json` | What the ERP must be able to **record and find**: field × operation the merchant needs but the Xentral API cannot do today, each with the business reason ("a clerk must be able to set and correct the customer's PO number"). |

Two files, one specification, two axes. Both are hand-written. Neither is
generated from the code — a specification derived from the implementation cannot
state what is still missing, which is the only reason anyone would review it.

Gaps recorded in `priorities.json` are rendered into the live `describe` output at
the field they concern, so a builder sees *"not possible, and here is why"* instead
of mere absence. Absence is indistinguishable from "nobody looked".

## Written by the machine — the result

| File | What it says |
|---|---|
| `verified.json` | What a live run against a real tenant actually demonstrated. Header records which instance and when. **Never edit by hand.** |
| `verified.xlsx` | The readable view of the same data, for review. Regenerate with `scripts/export_verified_xlsx.py`. |

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

## Built by developers

| File | Role |
|---|---|
| `emulated/*.py` | The implementation, one module per business object. |
| `manifest.py` | Which adapters this core exposes. |
| `checks/verify.py` | The prober: runs against a real tenant and writes `verified.json`. |
| `descriptions.json` | Field descriptions surfaced in `describe`. |
| `playbook.md` | Short prose for agents building workflows. Checked against the core by CI. |
| `docs/` | Investigations and findings behind individual decisions. |

## The loop

1. The expert states in the **spec** what has to be possible.
2. Developers build it in **`emulated/`**.
3. **`checks/verify.py`** runs against a real tenant and writes **`verified.json`**.
4. The **review sheet** puts requirement, implementation and evidence side by side.
5. The expert reads it: still missing anything? Is a documented gap acceptable?
6. CI fails whenever spec and core drift apart — in *both* directions: something
   required but not built, and something built that no spec mentions.

Sign-off is recorded in the spec's `reviewed` section. It is empty today: nothing
in this core has been through a domain review yet, and the sheet has to be able to
say so rather than imply a check that never happened.

## Checks

```bash
python scripts/validate_cores.py
PYTHONPATH=<agent-os>/backend pytest tests/test_core_playbooks.py
```
