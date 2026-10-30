#!/usr/bin/env python3
"""Replace the results tables in the report and README from the harness output.

Run after ``python3 backend/evaluate.py --quick > docs/eval_output.txt``:

    python3 docs/apply_eval_tables.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from format_eval_table import parse  # noqa: E402

ROOT = HERE.parent
REPORT = ROOT / "docs" / "REVIEW_DOCUMENT.md"
README = ROOT / "README.md"
OUTPUT = ROOT / "docs" / "eval_output.txt"


def table_md(rows: list[list[str]], include_viol: bool) -> list[str]:
    if include_viol:
        out = [
            "| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | viol | reduced | after | recov(d) |",
            "|----------|---------|-----|----------|-----------|-------------|---------|------|------|---------|-------|----------|",
        ]
    else:
        out = [
            "| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | reduced | after | recov(d) |",
            "|----------|---------|-----|----------|-----------|-------------|---------|------|---------|-------|----------|",
        ]
    for r in rows:
        s, v, det, delay, atk, fp, fz, cpu, viol, red, aft, rec = r[:12]
        if include_viol:
            out.append(
                f"| {s} | {v} | {det} | {delay} | {atk} | {fp} | {fz} | {cpu} | {viol} | {red} | {aft} | {rec} |"
            )
        else:
            out.append(
                f"| {s} | {v} | {det} | {delay} | {atk} | {fp} | {fz} | {cpu} | {red} | {aft} | {rec} |"
            )
    return out


def replace_table(path: Path, marker: str, new_lines: list[str]) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(marker)), None)
    if start is None:
        return False
    end = start
    while end < len(lines) and lines[end].strip():
        end += 1
    lines[start:end] = new_lines
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def main() -> int:
    rows = parse(OUTPUT)
    if not rows:
        print(f"no rows parsed from {OUTPUT}", file=sys.stderr)
        return 1
    ok_report = replace_table(
        REPORT,
        "| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | viol |",
        table_md(rows, include_viol=True),
    )
    ok_readme = replace_table(
        README,
        "| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | reduced |",
        table_md(rows, include_viol=False),
    )
    print(f"report updated: {ok_report}; readme updated: {ok_readme}; rows: {len(rows)}")
    return 0 if (ok_report and ok_readme) else 1


if __name__ == "__main__":
    raise SystemExit(main())
