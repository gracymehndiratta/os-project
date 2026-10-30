#!/usr/bin/env python3
"""Convert evaluate.py output into the markdown rows used in the report/README.

Usage:
    python3 docs/format_eval_table.py docs/eval_output.txt [--no-viol]

The harness prints 15 whitespace-separated columns; this script validates the
shape and emits the reduced column set used in the documents.
"""
from __future__ import annotations

import sys
from pathlib import Path

VARIANTS = {"static", "plain_adaptive", "gate_only", "budget_only", "full", "iforest"}
N_COLS = 15


def parse(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != N_COLS:
            continue
        if parts[0] == "scenario" or parts[1] not in VARIANTS:
            continue
        rows.append(parts)
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    include_viol = "--no-viol" not in sys.argv
    rows = parse(Path(sys.argv[1]))
    if not rows:
        print("no rows parsed", file=sys.stderr)
        return 1

    if include_viol:
        print("| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | viol | reduced | after | recov(d) |")
        print("|----------|---------|-----|----------|-----------|-------------|---------|------|------|---------|-------|----------|")
    else:
        print("| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | reduced | after | recov(d) |")
        print("|----------|---------|-----|----------|-----------|-------------|---------|------|---------|-------|----------|")

    for r in rows:
        scenario, variant, det, delay, atk, fp, freezes, cpu, viol, reduced, after, recov = r[:12]
        if include_viol:
            print(f"| {scenario} | {variant} | {det} | {delay} | {atk} | {fp} | {freezes} | {cpu} | {viol} | {reduced} | {after} | {recov} |")
        else:
            print(f"| {scenario} | {variant} | {det} | {delay} | {atk} | {fp} | {freezes} | {cpu} | {reduced} | {after} | {recov} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
