#!/usr/bin/env python3
"""Render captured command output into terminal-styled HTML pages.

Reads docs/tests_output.txt and docs/eval_output.txt (produced by running the
commands) and writes docs/terminal_tests.html and docs/terminal_eval.html.
These are opened in a browser and screenshotted for the report.

Run:  python3 docs/make_terminal_pages.py
"""
from __future__ import annotations

import html
from pathlib import Path

DOCS = Path(__file__).resolve().parent

TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  html, body {{ margin: 0; padding: 0; background: #0b1020; }}
  .win {{
    width: 1180px; margin: 28px auto; border-radius: 10px; overflow: hidden;
    box-shadow: 0 18px 50px rgba(0,0,0,.55); font-family: "SF Mono", Menlo,
    Consolas, "Courier New", monospace;
  }}
  .bar {{ background: #2d3348; padding: 10px 14px; display: flex;
          align-items: center; gap: 8px; }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .r {{ background: #ff5f57; }} .y {{ background: #febc2e; }} .g {{ background: #28c840; }}
  .title {{ color: #cbd5e0; font-size: 13px; margin-left: 10px; }}
  pre {{
    margin: 0; padding: 18px 22px; background: #11162a; color: #d7e0ea;
    font-size: 13px; line-height: 1.45; white-space: pre-wrap; word-break: break-word;
  }}
  .ok {{ color: #4ade80; font-weight: bold; }}
</style></head>
<body>
  <div class="win">
    <div class="bar">
      <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
      <span class="title">{title}</span>
    </div>
    <pre>{body}</pre>
  </div>
</body></html>
"""


def build(src_name: str, out_name: str, title: str) -> None:
    src = DOCS / src_name
    if not src.exists():
        print(f"skip (missing): {src}")
        return
    text = src.read_text(encoding="utf-8").rstrip("\n")
    body = html.escape(text)
    # highlight the final "OK" / summary lines
    body = body.replace("OK", '<span class="ok">OK</span>')
    (DOCS / out_name).write_text(TEMPLATE.format(title=title, body=body), encoding="utf-8")
    print(f"wrote {DOCS / out_name}")


def main() -> None:
    build("tests_output.txt", "terminal_tests.html", "backend — python -m unittest discover -s tests")
    build("eval_output.txt", "terminal_eval.html", "backend — python evaluate.py --quick")


if __name__ == "__main__":
    main()
