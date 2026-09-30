"""Scan the built PDFs for web markup that the conversion did not handle.

Usage: .venv/Scripts/python build/qa.py
"""
import glob
import re
import sys

import pymupdf

sys.stdout.reconfigure(encoding="utf-8")

PATTERNS = {
    "fence": r"^\s*(:::|```|~~~)",
    "tab": r'===\s+"',
    "admonition": r"^\s*(!!!|\?\?\?)",
    "snippet": r"--8<--",
    "html": r"</?(div|span|img|table|details|summary|br|p)\b",
    "attr-list": r"\{\s*[:.#][\w-]",
    "icon": r":(material|octicons|fontawesome|simple)-",
    "jinja": r"\{\{[%$]",
    "md-link": r"\]\((http|\.\./|/|#)",
    "md-image": r"!\[",
}

for log in sorted(glob.glob("build/out/vol*.log")):
    text = open(log, encoding="utf-8", errors="replace").read()
    lost = re.findall(r"Missing character: There is no (.+?) in font ([^!]+)!", text)
    if lost:
        print(f"{log}: {len(lost)} missing glyphs, for example {sorted(set(lost))[:8]}")

for f in sorted(glob.glob("dist/*.pdf")):
    d = pymupdf.open(f)
    print(f"{f}  ({len(d)} pages)")
    for name, p in PATTERNS.items():
        hits = []
        for i, pg in enumerate(d):
            for ln in pg.get_text().splitlines():
                if re.search(p, ln):
                    hits.append((i + 1, ln.strip()[:100]))
        if hits:
            print(f"  {name}: {len(hits)}")
            for h in hits[:6]:
                print("     p.%d  %s" % h)
    # Text below the bottom margin (1 in) that is not the footer: a box that did
    # not break across pages. LaTeX does not log this for tcolorbox.
    spill = []
    for i, pg in enumerate(d):
        limit = pg.rect.height - 72 + 6
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    if sp["bbox"][3] > limit and t and not t.isdigit() and not re.fullmatch(r"[ivxlc]+", t) and "docs " not in t:
                        spill.append(i + 1)
    if spill:
        print(f"  text below bottom margin: pages {sorted(set(spill))}")
    nop = [i + 1 for i, pg in enumerate(d) if "Not available in ADK Python" in pg.get_text()]
    print("  'Not available in ADK Python' on pages:", nop)
