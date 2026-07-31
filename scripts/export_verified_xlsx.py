"""Render a core's live schema + verified.json into a readable Excel workbook.

`verified.json` records only *results*, so reading it alone cannot tell you what
was never tested — the untested fields simply are not in it. This joins the two
sides: the schema supplies the rows (what the core declares), the manifest colours
the cells (what a live run proved).

Nothing here talks to a tenant. `adapter.metadata()` already stamps everything the
workbook needs — `verified` (per-facet verdicts + `<facet>Note`), `priority` (the
blue wishes from priorities.json) and `description` — so the export is a pure
offline read of the checked-in files.

Cell vocabulary, per field × facet:

    ok       the facet was probed and passed
    FEHLER   probed and failed — the note says why
    offen    the core declares the facet, but no run has proven it
    –        the schema does not declare the facet for this field (not applicable)
    Wunsch   a blue wish: declared as not-possible, with a reason

That distinction is the point of the sheet. "offen" and "–" look the same in the
JSON (both absent) and mean opposite things.

Run (openpyxl is deliberately not a repo dependency — supplied per call, like the
test runs supply pytest):

    PYTHONPATH=<agent-os>/backend \\
      uv run --project <agent-os>/backend --with openpyxl \\
      python scripts/export_verified_xlsx.py [core_id]
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CORES = _ROOT / "cores"

# Cores import as `xentral_entity_cores.<id>` (never top-level — a core id like
# `xentral_api` would shadow a same-named backend module). Mirrors conftest.py.
_PKG = "xentral_entity_cores"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__package__ = _PKG
    _pkg.__path__ = [str(_CORES)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = _pkg

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

# The facet vocabulary the runner records, in the order the backend surfaces it
# (entity_registry/verification.py::CAPABILITY_FACETS).
FACETS = ("read", "create", "update", "filter", "sort", "search")

# facet → the schema flag that says whether it applies to a field at all.
# `read` has no flag: everything declared is readable, including readOnly fields.
_FLAG = {
    "create": "creatable",
    "update": "updatable",
    "filter": "filterable",
    "sort": "sortable",
    "search": "searchable",
}

OK, FAIL, OPEN, NA, WISH = "ok", "FEHLER", "offen", "–", "Wunsch"

_FILL = {
    OK: PatternFill("solid", fgColor="C6E0B4"),
    FAIL: PatternFill("solid", fgColor="F4B183"),
    OPEN: PatternFill("solid", fgColor="EDEDED"),
    NA: PatternFill("solid", fgColor="FFFFFF"),
    WISH: PatternFill("solid", fgColor="BDD7EE"),
}
_HEAD = PatternFill("solid", fgColor="44546A")
_HEAD_FONT = Font(color="FFFFFF", bold=True)
_TITLE_FONT = Font(bold=True, size=12)


def _cell(spec: dict[str, Any], facet: str) -> tuple[str, str | None]:
    """One field × facet → (value, note)."""
    wish = (spec.get("priority") or {}).get(facet)
    if wish:
        return WISH, wish
    verified = spec.get("verified") or {}
    note = verified.get(f"{facet}Note")
    verdict = verified.get(facet)
    if verdict == "pass":
        return OK, note
    if verdict == "fail":
        return FAIL, note or "probe failed"
    flag = _FLAG.get(facet)
    if flag and not spec.get(flag):
        # Not declared for this field — asking whether it was tested is meaningless.
        # A leftover note here is worth keeping though: the runner only writes one
        # for a facet it considered applicable, so it means the schema has since
        # dropped the flag and the manifest is stale on that cell.
        return NA, (f"veraltet? {note}" if note else None)
    return OPEN, note


def _walk(props: Any, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every declared path, containers included, so `addresses` and
    `addresses.city` both get a row. Recurses embedded `properties` AND collection
    `node.properties` — the same shape verify.py::_field_paths walks."""
    out: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(props, dict):
        return out
    for name, spec in sorted(props.items()):
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}.{name}" if prefix else name
        out.append((path, spec))
        sub = spec.get("properties")
        if not isinstance(sub, dict):
            node = spec.get("node")
            sub = node.get("properties") if isinstance(node, dict) else None
        out += _walk(sub, path)
    return out


_BAD_TITLE = re.compile(r"[\[\]:*?/\\]")


def _sheet_title(key: str, used: set[str]) -> str:
    """Excel: max 31 chars, no []:*?/\\ , unique. The full key stays on the
    overview sheet, so a shortened tab never loses information."""
    title = _BAD_TITLE.sub("-", key)[:31]
    if title not in used:
        used.add(title)
        return title
    for n in range(2, 100):
        cand = f"{title[: 31 - len(str(n)) - 1]}~{n}"
        if cand not in used:
            used.add(cand)
            return cand
    raise RuntimeError(f"no unique sheet title for {key}")


def _write_header(ws: Any, row: int, headers: list[str]) -> None:
    for col, head in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col, value=head)
        c.fill = _HEAD
        c.font = _HEAD_FONT
        c.alignment = Alignment(vertical="center")


def _autosize(ws: Any, widths: dict[int, int]) -> None:
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width


def _entity_sheet(wb: Workbook, key: str, meta: dict[str, Any], title: str) -> dict[str, Any]:
    ws = wb.create_sheet(title)
    ws.cell(row=1, column=1, value=f"{key} — {meta.get('label') or ''}").font = _TITLE_FONT
    ws.cell(row=2, column=1, value="Operations: " + ", ".join(meta.get("operations") or []))

    headers = ["Pfad", "Typ", "Label", *FACETS, "Beschreibung", "Notizen"]
    _write_header(ws, 4, headers)
    ws.freeze_panes = "A5"

    props = (meta.get("rootNode") or {}).get("properties") or {}
    tally = {v: 0 for v in (OK, FAIL, OPEN, NA, WISH)}
    row = 5
    for path, spec in _walk(props):
        notes: list[str] = []
        ws.cell(row=row, column=1, value=path)
        ws.cell(row=row, column=2, value=spec.get("type"))
        ws.cell(row=row, column=3, value=spec.get("label"))
        for i, facet in enumerate(FACETS):
            value, note = _cell(spec, facet)
            tally[value] += 1
            c = ws.cell(row=row, column=4 + i, value=value)
            c.fill = _FILL[value]
            c.alignment = Alignment(horizontal="center")
            if note:
                notes.append(f"{facet}: {note}")
        ws.cell(row=row, column=10, value=spec.get("description"))
        ws.cell(row=row, column=11, value=" | ".join(notes))
        row += 1

    field_rows = row - 5
    ws.auto_filter.ref = f"A4:K{max(row - 1, 4)}"

    def _block(start: int, heading: str, cols: list[str], rows: list[list[Any]]) -> int:
        ws.cell(row=start, column=1, value=heading).font = _TITLE_FONT
        _write_header(ws, start + 1, cols)
        r = start + 2
        for values in rows:
            for ci, v in enumerate(values, start=1):
                c = ws.cell(row=r, column=ci, value=v)
                if ci == len(cols) - 1 and v in _FILL:
                    c.fill = _FILL[v]
                    c.alignment = Alignment(horizontal="center")
            r += 1
        if not rows:
            ws.cell(row=r, column=1, value="(keine)")
            r += 1
        return r + 1

    def _status(entry: dict[str, Any]) -> tuple[str, str | None]:
        if entry.get("wish"):
            return WISH, entry["wish"]
        v = (entry.get("verified") or {}).get("status")
        if v == "pass":
            return OK, None
        if v == "fail":
            return FAIL, None
        return OPEN, None

    act_rows = []
    for a in meta.get("actions") or []:
        status, note = _status(a)
        act_rows.append(
            [
                a.get("key"),
                a.get("label"),
                "ja" if a.get("destructive") else "",
                a.get("description") or "",
                status,
                note or "",
            ]
        )
    nxt = _block(
        row + 1,
        "Actions",
        ["Key", "Label", "destruktiv", "Beschreibung", "Status", "Notiz"],
        act_rows,
    )

    step_rows = []
    for grp in meta.get("processSteps") or []:
        for cmd in (grp or {}).get("commands") or []:
            status, note = _status(cmd)
            step_rows.append(
                [
                    cmd.get("key"),
                    f"{grp.get('label') or grp.get('group')} › {cmd.get('label')}",
                    "ja" if cmd.get("destructive") else "",
                    cmd.get("description") or "",
                    status,
                    note or "",
                ]
            )
    _block(
        nxt,
        "Process-Steps",
        ["Key", "Label", "destruktiv", "Beschreibung", "Status", "Notiz"],
        step_rows,
    )

    _autosize(ws, {1: 34, 2: 12, 3: 22, 4: 8, 5: 8, 6: 8, 7: 8, 8: 8, 9: 8, 10: 60, 11: 70})

    def _count(rows: list[list[Any]], value: str) -> int:
        return sum(1 for r in rows if r[4] == value)

    return {
        "fields": field_rows,
        "tally": tally,
        "actions": (len(act_rows), _count(act_rows, OK), _count(act_rows, FAIL)),
        "steps": (len(step_rows), _count(step_rows, OK), _count(step_rows, FAIL)),
    }


def main(core_id: str = "agentos_neo_xentral") -> int:
    core_pkg = __import__(f"{_PKG}.{core_id}", fromlist=["CORE"])
    core = core_pkg.CORE

    manifest_path = _CORES / core_id / "verified.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # verify.py writes generatedAt as None, so the file's mtime is the only
    # honest timestamp available.
    stamped = dt.datetime.fromtimestamp(manifest_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    wb = Workbook()
    overview = wb.active
    overview.title = "Übersicht"

    used: set[str] = set()
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for adapter in core.adapters:
        key = adapter.manifest.key
        meta = adapter.metadata()
        title = _sheet_title(key, used)
        rows.append((key, title, _entity_sheet(wb, key, meta, title)))

    overview.cell(row=1, column=1, value=f"Kern: {core_id}").font = _TITLE_FONT
    overview.cell(row=2, column=1, value=f"Instanz: {manifest.get('instance') or '?'}")
    overview.cell(row=3, column=1, value=f"verified.json zuletzt geschrieben: {stamped}")
    overview.cell(
        row=4,
        column=1,
        value=(
            "ok = geprüft und bestanden · FEHLER = geprüft und fehlgeschlagen · "
            "offen = deklariert, aber ungeprüft · – = laut Schema nicht anwendbar · "
            "Wunsch = bewusst nicht möglich (priorities.json)"
        ),
    )

    headers = [
        "Entity",
        "Tab",
        "Felder",
        "ok",
        "FEHLER",
        "offen",
        "–",
        "Wunsch",
        "Actions (ok/Fehler)",
        "Steps (ok/Fehler)",
    ]
    _write_header(overview, 6, headers)
    overview.freeze_panes = "A7"
    r = 7
    for key, title, stats in sorted(rows):
        t = stats["tally"]
        a_all, a_ok, a_fail = stats["actions"]
        s_all, s_ok, s_fail = stats["steps"]
        for ci, v in enumerate(
            [
                key,
                title,
                stats["fields"],
                t[OK],
                t[FAIL],
                t[OPEN],
                t[NA],
                t[WISH],
                f"{a_all} ({a_ok}/{a_fail})",
                f"{s_all} ({s_ok}/{s_fail})",
            ],
            start=1,
        ):
            overview.cell(row=r, column=ci, value=v)
        r += 1
    overview.auto_filter.ref = f"A6:J{max(r - 1, 6)}"
    _autosize(overview, {1: 28, 2: 28, 3: 9, 4: 8, 5: 9, 6: 8, 7: 6, 8: 9, 9: 20, 10: 20})

    out = _CORES / core_id / "verified.xlsx"
    wb.save(out)
    total = sum(s["fields"] for _, _, s in rows)
    fails = sum(s["tally"][FAIL] for _, _, s in rows)
    print(f"{len(rows)} Entities, {total} Feldzeilen, {fails} FEHLER-Zellen → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:2]))
