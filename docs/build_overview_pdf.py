#!/usr/bin/env python3
"""Render docs/PROJECT_OVERVIEW.md to a polished PDF.

Reuses the small Markdown subset parser from ``build_report.py`` so the
project-overview document renders with the same conventions as the main
report.  The HTML is printed to PDF with headless Chrome.

Run:  python3 docs/build_overview_pdf.py
"""
from __future__ import annotations

import html
import subprocess
import sys
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent
MD_PATH = DOCS / "PROJECT_OVERVIEW.md"
HTML_PATH = DOCS / "PROJECT_OVERVIEW.html"
PDF_PATH = DOCS / "OS_Behavior_Monitor_Project_Overview.pdf"

sys.path.insert(0, str(DOCS))
from build_report import inline_html, parse_markdown  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CSS = """
@page { size: A4; margin: 17mm 15mm 18mm 15mm; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html { background: #ffffff; }
body {
  margin: 0; color: #1f2937; background: #ffffff;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 10.4pt; line-height: 1.52;
}
.cover {
  min-height: 250mm; display: flex; flex-direction: column; justify-content: center;
  text-align: left; padding: 10mm 4mm; page-break-after: always;
}
.cover .eyebrow {
  font-size: 9pt; letter-spacing: 0.22em; text-transform: uppercase;
  color: #2563eb; font-weight: 700; margin-bottom: 14pt;
}
.cover h1 {
  font-size: 34pt; line-height: 1.1; margin: 0 0 10pt 0; color: #0f172a;
  letter-spacing: -0.02em; border: none; padding: 0;
}
.cover .subtitle {
  font-size: 14.5pt; line-height: 1.35; color: #334155; font-weight: 500;
  margin: 0 0 24pt 0; max-width: 150mm;
}
.cover .rule { width: 60mm; height: 4px; background: #2563eb; margin: 0 0 24pt 0; }
.cover .meta { font-size: 9.6pt; color: #475569; line-height: 1.8; }
.cover .meta strong { color: #0f172a; }
.cover .abstract {
  margin-top: 30pt; padding: 14pt 16pt; border-left: 3px solid #2563eb;
  background: #f8fafc; color: #334155; font-size: 10pt; line-height: 1.6;
  max-width: 160mm;
}
h2 {
  font-size: 15.5pt; color: #0f172a; margin: 20pt 0 8pt 0; padding-bottom: 4pt;
  border-bottom: 1.5px solid #cbd5e1; page-break-after: avoid; letter-spacing: -0.01em;
}
h3 {
  font-size: 11.6pt; color: #1d4ed8; margin: 14pt 0 5pt 0;
  page-break-after: avoid;
}
p { margin: 6pt 0; orphans: 3; widows: 3; }
strong { color: #0f172a; }
code {
  font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.9pt;
  background: #f1f5f9; color: #0f172a; padding: 1px 4px; border-radius: 3px;
  border: 1px solid #e2e8f0;
}
pre {
  background: #f8fafc; border: 1px solid #dbe3ee; border-left: 3px solid #2563eb;
  border-radius: 5px; padding: 9pt 11pt; overflow-x: auto;
  page-break-inside: avoid; margin: 8pt 0;
}
pre code { background: none; border: none; padding: 0; font-size: 8.4pt; line-height: 1.4; }
table {
  border-collapse: collapse; width: 100%; margin: 8pt 0 11pt 0;
  font-size: 8.8pt; page-break-inside: auto;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th, td {
  border: 1px solid #d5dde8; padding: 4.5pt 6pt; text-align: left; vertical-align: top;
}
th { background: #eef4fb; color: #0f172a; font-weight: 650; }
tbody tr:nth-child(even) td { background: #fbfdff; }
ul, ol { margin: 6pt 0; padding-left: 19pt; }
li { margin: 2.6pt 0; }
hr { border: none; border-top: 1px solid #dbe3ee; margin: 13pt 0; }
"""


def render() -> str:
    blocks = parse_markdown(MD_PATH.read_text(encoding="utf-8"))
    parts: list[str] = []
    parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    parts.append("<title>OS Behavior Monitor - Complete Project Overview</title>")
    parts.append(f"<style>{CSS}</style></head><body>")

    parts.append(
        """
<div class="cover">
  <div class="eyebrow">Operating Systems Project &middot; October 2026</div>
  <h1>OS Behavior Monitor</h1>
  <div class="subtitle">Divergence-Gated Behavioral Baseline Learning with
  Overhead-Budgeted Adaptive Sampling</div>
  <div class="rule"></div>
  <div class="meta">
    <strong>Team:</strong> Gracy Mehndiratta (24BCE2987) and Manan Kulshrestha (24BCE0262)<br>
    <strong>Repository:</strong> github.com/gracymehndiratta/os-project<br>
    <strong>Document:</strong> Complete project overview, algorithms, and technology stack
  </div>
  <div class="abstract">
    A per-tick operating-system monitoring agent that learns per-(application, context)
    behavioral baselines, detects anomalies with a joint multi-resource Mahalanobis score,
    protects those baselines from slow poisoning through a divergence gate with quarantine
    and independent corroboration, and keeps its own CPU overhead inside a target budget
    using adaptive tiered sampling.
  </div>
</div>
"""
    )

    # The cover already carries the title and metadata, so begin the body at
    # the first numbered section instead of repeating the front matter.
    start = next(
        (
            i
            for i, b in enumerate(blocks)
            if b["type"] == "heading" and b["text"].startswith("1. ")
        ),
        0,
    )
    for block in blocks[start:]:
        btype = block["type"]
        if btype == "heading":
            level = min(block["level"], 4)
            parts.append(f"<h{level}>{inline_html(block['text'])}</h{level}>")
        elif btype == "para":
            parts.append(f"<p>{inline_html(block['text'])}</p>")
        elif btype == "bullets":
            parts.append("<ul>" + "".join(f"<li>{inline_html(x)}</li>" for x in block["items"]) + "</ul>")
        elif btype == "numbers":
            parts.append("<ol>" + "".join(f"<li>{inline_html(x)}</li>" for x in block["items"]) + "</ol>")
        elif btype == "code":
            parts.append(f"<pre><code>{html.escape(block['text'])}</code></pre>")
        elif btype == "table":
            head = "".join(f"<th>{inline_html(c)}</th>" for c in block["header"])
            rows = "".join(
                "<tr>" + "".join(f"<td>{inline_html(c)}</td>" for c in row) + "</tr>"
                for row in block["rows"]
            )
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        elif btype == "image":
            src = html.escape(block["src"])
            alt = html.escape(block["alt"])
            parts.append(f"<figure><img src='{src}' alt='{alt}'><figcaption>{alt}</figcaption></figure>")
        elif btype == "hr":
            parts.append("<hr>")

    parts.append("</body></html>")
    return "\n".join(parts)


def main() -> int:
    if not MD_PATH.exists():
        print(f"missing {MD_PATH}", file=sys.stderr)
        return 1
    if not Path(CHROME).exists():
        print(f"Chrome not found at {CHROME}", file=sys.stderr)
        return 1

    doc = render()
    HTML_PATH.write_text(doc, encoding="utf-8")
    print(f"wrote {HTML_PATH}")

    with tempfile.TemporaryDirectory(prefix="osmon-pdf-") as profile:
        cmd = [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={PDF_PATH}",
            HTML_PATH.resolve().as_uri(),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not PDF_PATH.exists():
        print("Chrome did not produce the PDF", file=sys.stderr)
        return 1
    print(f"wrote {PDF_PATH} ({PDF_PATH.stat().st_size / 1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
