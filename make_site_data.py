"""Build site/data.js from the recorded runs. Read-only on results and the attack library."""
import ast
import json
from pathlib import Path
from typing import Any

import run

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site" / "data.js"
PREFIX = "window.TRIPWIRE_DATA = "
SINGLE_RUNS = [
    "none.json",
    "only_delimiters.json",
    "only_flag_phrases.json",
    "only_output_filter.json",
    "only_schema.json",
    "only_strip_hidden.json",
    "all_defences.json",
]
LABELS = {
    "none.json": "No defences",
    "only_delimiters.json": "Delimiters only",
    "only_flag_phrases.json": "Flag phrases only",
    "only_output_filter.json": "Output filter only",
    "only_schema.json": "Schema only",
    "only_strip_hidden.json": "Strip hidden only",
    "all_defences.json": "All defences",
}


def cell_state(row: dict) -> str:
    """Same rule as app.cell_state: strict success, mentions only, blocked, or no reply."""
    if row.get("verdict") == "no reply" or not (row.get("reply") or "").strip():
        return "no reply"
    if row.get("verdict") == "success":
        return "strict success"
    return "mentions only" if row.get("mentions") == "success" else "blocked"


def app_constants(app_path: Path) -> dict[str, Any]:
    """Read CAVEATS and SINGLE_RUNS_NOTE from app.py source without importing streamlit."""
    tree = ast.parse(Path(app_path).read_text(encoding="utf-8"))
    wanted = {"CAVEATS", "SINGLE_RUNS_NOTE"}
    return {
        t.id: ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name) and t.id in wanted
    }


def build(results_dir: Path, library_path: Path, app_path: Path) -> dict:
    library = json.loads(Path(library_path).read_text(encoding="utf-8"))
    attacks = [a for a in library if a["technique"] != "control"]
    consts = app_constants(app_path)
    runs = []
    for name in SINGLE_RUNS:
        record = run.load_run(Path(results_dir) / name)
        by_id = {r["id"]: r for r in record["results"]}
        cells = []
        for a in attacks:
            row = by_id.get(a["id"], {})
            cells.append(
                {
                    "id": a["id"],
                    "state": cell_state(row) if row else "no reply",
                    "verdict": row.get("verdict", "no reply"),
                    "mentions": row.get("mentions"),
                    "reply": row.get("reply") or "",
                }
            )
        runs.append(
            {
                "file": name,
                "label": LABELS[name],
                "model": record.get("model", ""),
                "defences": record.get("defences", []),
                "summary": run.summarize(record),
                "cells": cells,
            }
        )
    return {
        "runs": runs,
        "attacks": [{k: a.get(k) for k in ("id", "technique", "subject", "body")} for a in attacks],
        "caveats": [c.replace("**", "") for c in consts["CAVEATS"]],
        "singleRunsNote": consts["SINGLE_RUNS_NOTE"],
    }


def render_js(data: dict) -> str:
    """JSON with <, >, & and line separators escaped so no string can end the script block."""
    body = json.dumps(data, ensure_ascii=True, indent=1, sort_keys=True)
    escapes = {"<": r"\u003c", ">": r"\u003e", "&": r"\u0026", chr(0x2028): r"\u2028", chr(0x2029): r"\u2029"}
    for ch, esc in escapes.items():
        body = body.replace(ch, esc)
    return PREFIX + body + ";\n"


def main() -> int:
    data = build(ROOT / "results", ROOT / "attacks" / "library.json", ROOT / "app.py")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render_js(data), encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(data['runs'])} runs, {len(data['attacks'])} attacks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
