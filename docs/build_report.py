#!/usr/bin/env python3
"""Render docs/REVIEW_DOCUMENT.md into a DOCX report and a print-ready HTML.

The Markdown source is the single source of truth.  This script parses a
small, well-defined subset of Markdown (headings, paragraphs, bullet and
numbered lists, fenced code blocks, pipe tables, images, horizontal rules and
inline ``**bold**`` / ``` `code` ``` spans) and emits:

* ``docs/OS_Behavior_Monitor_Report.docx``  (python-docx)
* ``docs/report.html``                       (print-ready, used for PDF)

Run:  python3 docs/build_report.py
"""
from __future__ import annotations

import html
import os
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
MD_PATH = DOCS / "REVIEW_DOCUMENT.md"
DOCX_PATH = DOCS / "OS_Behavior_Monitor_Report.docx"
HTML_PATH = DOCS / "report.html"


# --------------------------------------------------------------------------
# Markdown parsing
# --------------------------------------------------------------------------
def parse_markdown(text: str) -> list[dict]:
    lines = text.splitlines()
    blocks: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            blocks.append({"type": "code", "text": "\n".join(buf), "lang": lang})
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # Heading
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            i += 1
            continue

        # Image (standalone line)
        m = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", stripped)
        if m:
            blocks.append({"type": "image", "alt": m.group(1), "src": m.group(2)})
            i += 1
            continue

        # Table
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            header = _split_row(stripped)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        # Bullet list
        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < n and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "bullets", "items": items})
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < n and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "numbers", "items": items})
            continue

        # Paragraph (consume until blank / block start)
        buf = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("```") or nxt in ("---", "***", "___")
                    or re.match(r"^#{1,6}\s", nxt) or re.match(r"^[-*]\s+", nxt)
                    or re.match(r"^\d+\.\s+", nxt) or re.match(r"^!\[", nxt)
                    or nxt.startswith("|")):
                break
            buf.append(nxt)
            i += 1
        blocks.append({"type": "para", "text": " ".join(buf)})

    return blocks


def _split_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


# --------------------------------------------------------------------------
# Inline formatting
# --------------------------------------------------------------------------
_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def inline_segments(text: str) -> list[tuple[str, str]]:
    """Return [(kind, text)] where kind is 'plain' | 'bold' | 'code'."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(("plain", text[pos:m.start()]))
        tok = m.group(0)
        if tok.startswith("**"):
            out.append(("bold", tok[2:-2]))
        else:
            out.append(("code", tok[1:-1]))
        pos = m.end()
    if pos < len(text):
        out.append(("plain", text[pos:]))
    return out or [("plain", text)]


def inline_html(text: str) -> str:
    parts = []
    for kind, chunk in inline_segments(text):
        esc = html.escape(chunk)
        if kind == "bold":
            parts.append(f"<strong>{esc}</strong>")
        elif kind == "code":
            parts.append(f"<code>{esc}</code>")
        else:
            parts.append(esc)
    return "".join(parts)


# --------------------------------------------------------------------------
# DOCX rendering
# --------------------------------------------------------------------------
def render_docx(blocks: list[dict], out_path: Path) -> None:
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # Base style
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    def shade(paragraph, fill: str) -> None:
        p_pr = paragraph._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        p_pr.append(shd)

    def add_runs(paragraph, text: str) -> None:
        for kind, chunk in inline_segments(text):
            run = paragraph.add_run(chunk)
            if kind == "bold":
                run.bold = True
            elif kind == "code":
                run.font.name = "Consolas"
                run.font.size = Pt(10)

    first_h2_done = False
    for block in blocks:
        btype = block["type"]

        if btype == "heading":
            level = block["level"]
            text = block["text"]
            if level == 1:
                h = doc.add_heading("", level=0)
                add_runs(h, text)
            else:
                if level == 2 and not first_h2_done:
                    doc.add_page_break()
                    first_h2_done = True
                elif level == 2:
                    doc.add_page_break()
                h = doc.add_heading("", level=min(level, 4))
                add_runs(h, text)

        elif btype == "para":
            p = doc.add_paragraph()
            add_runs(p, block["text"])

        elif btype == "bullets":
            for item in block["items"]:
                p = doc.add_paragraph(style="List Bullet")
                add_runs(p, item)

        elif btype == "numbers":
            for item in block["items"]:
                p = doc.add_paragraph(style="List Number")
                add_runs(p, item)

        elif btype == "code":
            for line in block["text"].split("\n"):
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.space_before = Pt(0)
                run = p.add_run(line if line else " ")
                run.font.name = "Consolas"
                run.font.size = Pt(9)
                shade(p, "F2F2F2")

        elif btype == "table":
            header = block["header"]
            rows = block["rows"]
            table = doc.add_table(rows=1, cols=len(header))
            table.style = "Table Grid"
            for j, cell_text in enumerate(header):
                cell = table.rows[0].cells[j]
                cell.text = ""
                p = cell.paragraphs[0]
                run = p.add_run(cell_text)
                run.bold = True
            for row in rows:
                cells = table.add_row().cells
                for j in range(len(header)):
                    val = row[j] if j < len(row) else ""
                    cells[j].text = ""
                    p = cells[j].paragraphs[0]
                    add_runs(p, val)

        elif btype == "image":
            src = DOCS / block["src"]
            if src.exists():
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.add_run().add_picture(str(src), width=Inches(6.0))
                cap = doc.add_paragraph()
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                cr = cap.add_run(block["alt"])
                cr.italic = True
                cr.font.size = Pt(9)
            else:
                doc.add_paragraph(f"[missing image: {block['src']}]")

        elif btype == "hr":
            p = doc.add_paragraph()
            p_pr = p._p.get_or_add_pPr()
            pbdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "999999")
            pbdr.append(bottom)
            p_pr.append(pbdr)

    doc.save(str(out_path))
    print(f"wrote {out_path}")


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------
CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a202c; line-height: 1.5; font-size: 11pt; margin: 0;
  padding: 0 6mm;
}
h1 { font-size: 26pt; margin: 0 0 4pt 0; color: #0b3d91; }
h2 { font-size: 17pt; color: #0b3d91; border-bottom: 2px solid #0b3d91;
     padding-bottom: 3pt; margin-top: 22pt; page-break-before: always; }
h2:first-of-type { page-break-before: avoid; }
h3 { font-size: 13pt; color: #14417a; margin-top: 16pt; }
h4 { font-size: 11.5pt; color: #14417a; }
p { margin: 6pt 0; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.5pt;
       background: #f2f4f7; padding: 1px 4px; border-radius: 3px; }
pre { background: #f2f4f7; border: 1px solid #e2e8f0; border-radius: 6px;
      padding: 10px 12px; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.8pt; line-height: 1.35; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0;
        page-break-inside: avoid; font-size: 9.5pt; }
th, td { border: 1px solid #cbd5e0; padding: 4px 6px; text-align: left;
         vertical-align: top; }
th { background: #edf2f7; font-weight: 600; }
img { max-width: 100%; height: auto; display: block; margin: 8pt auto;
      border: 1px solid #e2e8f0; border-radius: 6px; }
figure { margin: 10pt 0; text-align: center; }
figcaption { font-size: 9pt; color: #4a5568; font-style: italic; margin-top: 3pt; }
hr { border: none; border-top: 1px solid #cbd5e0; margin: 14pt 0; }
ul, ol { margin: 6pt 0 6pt 0; padding-left: 22pt; }
li { margin: 3pt 0; }
.cover { text-align: center; margin-top: 30mm; }
.cover h1 { font-size: 30pt; }
.cover .sub { font-size: 14pt; color: #2d3748; margin-bottom: 26pt; }
""".strip()


def render_html(blocks: list[dict], out_path: Path) -> None:
    parts: list[str] = []
    parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    parts.append("<title>OS Behavior Monitor — Implementation Report</title>")
    parts.append(f"<style>{CSS}</style></head><body>")

    for block in blocks:
        btype = block["type"]
        if btype == "heading":
            lvl = min(block["level"], 4)
            parts.append(f"<h{lvl}>{inline_html(block['text'])}</h{lvl}>")
        elif btype == "para":
            parts.append(f"<p>{inline_html(block['text'])}</p>")
        elif btype == "bullets":
            parts.append("<ul>" + "".join(f"<li>{inline_html(x)}</li>" for x in block["items"]) + "</ul>")
        elif btype == "numbers":
            parts.append("<ol>" + "".join(f"<li>{inline_html(x)}</li>" for x in block["items"]) + "</ol>")
        elif btype == "code":
            parts.append(f"<pre><code>{html.escape(block['text'])}</code></pre>")
        elif btype == "table":
            rows = "".join(
                "<tr>" + "".join(f"<td>{inline_html(c)}</td>" for c in r) + "</tr>"
                for r in block["rows"]
            )
            head = "".join(f"<th>{inline_html(c)}</th>" for c in block["header"])
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        elif btype == "image":
            src = block["src"]
            parts.append(
                f"<figure><img src='{html.escape(src)}' alt='{html.escape(block['alt'])}'>"
                f"<figcaption>{html.escape(block['alt'])}</figcaption></figure>"
            )
        elif btype == "hr":
            parts.append("<hr>")

    parts.append("</body></html>")
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out_path}")


def main() -> int:
    if not MD_PATH.exists():
        print(f"missing {MD_PATH}", file=sys.stderr)
        return 1
    blocks = parse_markdown(MD_PATH.read_text(encoding="utf-8"))
    render_docx(blocks, DOCX_PATH)
    render_html(blocks, HTML_PATH)
    print(f"parsed {len(blocks)} blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
