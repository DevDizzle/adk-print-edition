"""Build the ADK Print Edition (PDF volumes) from the google/adk-docs repo.

Usage:
    python build/build.py            # all volumes (clones src/adk-docs if missing)
    python build/build.py --pull     # get the newest docs first
    python build/build.py --vol 1    # one volume
    python build/build.py --tex-only # stop after the .tex files

Pipeline: volumes.yml -> preprocess each MkDocs page (snippets, Python-only
tabs, admonitions, links, images) -> one Markdown file per volume -> pandoc
(LaTeX, Lua filter) -> xelatex -> dist/*.pdf
"""
import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "src" / "adk-docs"
DOCS = REPO / "docs"
BUILD = ROOT / "build"
OUT = BUILD / "out"
IMG = OUT / "img"
DIST = ROOT / "dist"
TOOLS = ROOT / "tools"

IS_WIN = os.name == "nt"
# Windows per-user installs (winget pandoc, TinyTeX) are often not on PATH in
# a new shell. Look there after PATH.
PANDOC = shutil.which("pandoc") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Pandoc" / "pandoc.exe")
XELATEX = shutil.which("xelatex") or str(
    Path(os.environ.get("APPDATA", "")) / "TinyTeX" / "bin" / "windows" / "xelatex.exe")
MMDC = TOOLS / "node_modules" / ".bin" / ("mmdc.cmd" if IS_WIN else "mmdc")
DOCS_GIT = "https://github.com/google/adk-docs.git"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

SITE = "https://google.github.io/adk-docs/"
TEXT_WIDTH_IN = 6.0
RASTER_MAX_IN = 4.8

WARNINGS = []
STATS = {"tabs_dropped": 0, "no_python_groups": 0, "snippets": 0, "images": 0, "mermaid": 0}


def warn(msg):
    WARNINGS.append(msg)


# --------------------------------------------------------------------------
# Snippets (pymdownx.snippets): --8<-- "path[:section|:start:end]"
# --------------------------------------------------------------------------
SNIP_RE = re.compile(r'^(?P<ind>[ \t]*)-{1,}8<-{1,}[ \t]+"(?P<spec>[^"]+)"[ \t]*$')
MARK_RE = re.compile(r"--8<--\s*\[\s*(?:start|end)\s*:\s*[\w.-]+\s*\]")


def dedent_lines(lines):
    widths = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    n = min(widths) if widths else 0
    return [l[n:] if l.strip() else "" for l in lines]


def read_snippet(spec, page):
    parts = spec.split(":")
    rel = parts[0]
    f = REPO / rel
    if not f.exists():
        f = DOCS / rel
    if not f.exists():
        warn(f"{page}: missing snippet {spec}")
        return [f"# [missing snippet: {spec}]"]
    lines = f.read_text(encoding="utf-8").expandtabs(4).splitlines()
    if len(parts) >= 2 and parts[1] and not parts[1].isdigit():
        name = re.escape(parts[1])
        start_re = re.compile(r"--8<--\s*\[\s*start\s*:\s*%s\s*\]" % name)
        end_re = re.compile(r"--8<--\s*\[\s*end\s*:\s*%s\s*\]" % name)
        out, on, found = [], False, False
        for ln in lines:
            if start_re.search(ln):
                on, found = True, True
                continue
            if end_re.search(ln):
                on = False
                continue
            if on:
                out.append(ln)
        if not found:
            warn(f"{page}: snippet section not found {spec}")
        lines = dedent_lines(out)
    elif len(parts) >= 2 and parts[1].isdigit():
        start = int(parts[1])
        end = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else len(lines)
        lines = lines[start - 1:end]
    lines = [l for l in lines if not MARK_RE.search(l)]
    # Trim blank lines at both ends.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    STATS["snippets"] += 1
    return lines


def expand_snippets(lines, page, depth=0):
    out = []
    for ln in lines:
        m = SNIP_RE.match(ln)
        if not m:
            out.append(ln)
            continue
        ind = m["ind"]
        inc = read_snippet(m["spec"], page)
        if m["spec"].split(":")[0].endswith(".md") and depth < 5:
            inc = expand_snippets(inc, page, depth + 1)
        out.extend(ind + l if l.strip() else "" for l in inc)
    return out


# --------------------------------------------------------------------------
# Block structure: code fences, content tabs, admonitions
# --------------------------------------------------------------------------
FENCE_RE = re.compile(r"^(?P<ind>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`]*)$")
TAB_RE = re.compile(r'^(?P<ind>[ \t]*)===[+!]*[ \t]+"(?P<label>.*)"[ \t]*$')
ADM_RE = re.compile(
    r'^(?P<ind>[ \t]*)(?P<mark>!!!|\?\?\?\+?)(?:[ \t]*(?P<type>[A-Za-z][\w-]*))?(?:[ \t]+"(?P<title>.*)")?[ \t]*$'
)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")

LANG_WORDS = {"python", "typescript", "javascript", "js", "ts", "go", "golang", "java", "kotlin", "maven", "gradle"}
LANG_ALIAS = {
    "py": "python", "python3": "python", "pycon": "python",
    "sh": "bash", "shell": "bash", "console": "bash", "zsh": "bash", "shell-session": "bash",
    "ts": "typescript", "js": "javascript", "yml": "yaml", "jsonc": "json", "json5": "json",
    "txt": "", "text": "", "none": "", "plaintext": "", "plain": "", "output": "", "log": "",
    "ps1": "powershell", "pwsh": "powershell", "cmd": "dosbat", "bat": "dosbat",
    "docker": "dockerfile", "env": "bash", "dotenv": "bash",
}
KNOWN_LANGS = set()


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def strip_indent(line, n):
    i = 0
    while i < n and i < len(line) and line[i] == " ":
        i += 1
    return line[i:]


def collect_body(lines, i, ind):
    """Collect the indented body under a tab/admonition header at lines[i].
    The body is normally indented 4 more spaces. Some source pages use 1-3;
    the first body line sets the indent in that case."""
    first = next((l for l in lines[i + 1:] if l.strip()), "")
    step = indent_of(first) - ind if ind < indent_of(first) < ind + 4 else 4
    j = i + 1
    body = []
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "" or indent_of(ln) >= ind + step:
            body.append(ln)
            j += 1
        else:
            break
    while body and not body[-1].strip():
        body.pop()
        j -= 1
    return [strip_indent(l, ind + step) if l.strip() else "" for l in body], j


LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s+)\S")
LIST_WIDE_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])( {2,})(\S.*)$")
DIV_LINE_RE = re.compile(r"^\s*</?div\b[^>]*>\s*$")
IFRAME_RE = re.compile(r'<iframe\b[^>]*?\bsrc="([^"]+)"')


def container_indent(out, ind):
    """Pandoc reads a block indented 4+ spaces past its container as an
    indented code block. MkDocs (superfences) does not. Pull the block back to
    the content column of the list item or paragraph that holds it."""
    prev = next((l for l in reversed(out) if l.strip()), None)
    if prev is None:
        return ind
    m = LIST_ITEM_RE.match(prev)
    col = len(m.group(1)) + len(m.group(2)) + len(m.group(3)) if m else indent_of(prev)
    if ind >= col + 4 or (m and ind > col):
        return col
    return ind


def shift_left(line, n):
    if n <= 0:
        return line
    return line[n:] if line.startswith(" " * n) else line.lstrip(" ")


def video_url(src):
    m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]+)", src)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else html.unescape(src)


def tab_lang(label):
    first = re.split(r"[\s\-(/]+", label.strip().lower())[0]
    return first if first in LANG_WORDS else None


def fence_attrs(info):
    info = info.strip()
    lang, title = "", None
    if info.startswith("{"):
        inner = info.strip("{} ")
        lm = re.search(r"\.([\w+#-]+)", inner)
        lang = lm.group(1) if lm else ""
        tm = re.search(r'title="([^"]*)"', inner)
        title = tm.group(1) if tm else None
    else:
        bits = info.split(None, 1)
        lang = bits[0] if bits else ""
        rest = bits[1] if len(bits) > 1 else ""
        tm = re.search(r'title="([^"]*)"', rest)
        title = tm.group(1) if tm else None
    lang = lang.lower()
    lang = LANG_ALIAS.get(lang, lang)
    return lang, title


class Page:
    def __init__(self, path, ctx):
        self.path = path            # docs-relative, posix
        self.ctx = ctx              # global Book context
        self.dir = posixpath.dirname(path)

    # ---- tabs --------------------------------------------------------------
    def render_tabs(self, tabs, ind):
        langs = [tab_lang(label) for label, _ in tabs]
        has_lang = any(langs)
        keep = [(label, body, lang) for (label, body), lang in zip(tabs, langs)
                if lang is None or lang == "python"]
        STATS["tabs_dropped"] += len(tabs) - len(keep)
        pad = " " * ind
        out = [""]
        if has_lang and not any(lang == "python" for _, _, lang in keep):
            STATS["no_python_groups"] += 1
            out += [pad + "[No Python version of this content. See the online docs for other languages.]{.nopython}", ""]
        label_each = len(keep) > 1 or (len(keep) == 1 and keep[0][2] is None)
        for label, body, lang in keep:
            body_t = self.transform(body)
            if label_each:
                out += [pad + "[" + md_escape_label(label) + "]{.tablabel}", ""]
            out += [(pad + l) if l.strip() else "" for l in body_t]
            out.append("")
        return out

    # ---- admonitions -------------------------------------------------------
    def render_adm(self, m, body, ind):
        typ = (m["type"] or "note").lower()
        title = m["title"]
        if title is None:
            title = typ.capitalize()
        title = html.unescape(title).replace('"', "'")
        body_t = self.transform(body)
        pad = " " * ind
        out = ["", pad + '::: {.admonition .%s title="%s"}' % (typ, title), ""]
        out += [(pad + l) if l.strip() else "" for l in body_t]
        out += ["", pad + ":::", ""]
        return out

    # ---- mermaid -----------------------------------------------------------
    def render_mermaid(self, code_lines, ind):
        src = "\n".join(dedent_lines(code_lines))
        key = hashlib.sha1(src.encode()).hexdigest()[:12]
        png = IMG / f"mermaid-{key}.png"
        if not png.exists():
            mmd = IMG / f"mermaid-{key}.mmd"
            mmd.write_text(src, encoding="utf-8")
            r = subprocess.run(
                [str(MMDC), "-i", str(mmd), "-o", str(png), "-s", "3", "-b", "white",
                 "-p", str(OUT / "puppeteer.json")],
                capture_output=True, text=True)
            if r.returncode != 0 or not png.exists():
                warn(f"{self.path}: mermaid render failed: {r.stderr[-300:]}")
                return ["", " " * ind + "```", *code_lines, " " * ind + "```", ""]
        STATS["mermaid"] += 1
        w_px = Image.open(png).width / 3
        width = min(w_px / 96, TEXT_WIDTH_IN)
        return ["", " " * ind + f"![](img/{png.name}){{width={width:.2f}in}}", ""]

    # ---- main block pass ---------------------------------------------------
    def transform(self, lines):
        out = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            m = FENCE_RE.match(ln)
            if m:
                fence = m["fence"]
                ind = len(m["ind"])
                lang, title = fence_attrs(m["info"])
                body = []
                i += 1
                closed = False
                while i < len(lines):
                    # A closing fence has no info string and at most 3 more spaces of indent
                    # (CommonMark), so a ``` line inside the code itself does not close it.
                    c = re.match(r"^([ \t]*)(`{3,}|~{3,})[ \t]*$", lines[i])
                    if (c and c.group(2)[0] == fence[0] and len(c.group(2)) >= len(fence)
                            and len(c.group(1)) - ind <= 3):
                        i += 1
                        closed = True
                        break
                    body.append(lines[i])
                    i += 1
                if not closed:
                    if not any(b.strip() for b in body):
                        continue  # stray fence in the source: drop it
                    warn(f"{self.path}: unclosed code fence")
                if lang == "mermaid":
                    out += self.render_mermaid(body, ind)
                    continue
                if lang and KNOWN_LANGS and lang not in KNOWN_LANGS:
                    lang = ""
                attrs = []
                if lang:
                    attrs.append("." + lang)
                if title:
                    attrs.append('title="%s"' % title.replace('"', "'"))
                new_ind = container_indent(out, ind)
                pad = " " * new_ind
                open_line = pad + fence + ("{" + " ".join(attrs) + "}" if attrs else "")
                body = [emoji_map(shift_left(b, ind - new_ind)) for b in body]
                out += ["", open_line, *body, pad + fence, ""]
                continue
            m = TAB_RE.match(ln)
            if m:
                ind = len(m["ind"])
                tabs = []
                while i < len(lines):
                    m2 = TAB_RE.match(lines[i])
                    if not m2 or len(m2["ind"]) != ind:
                        break
                    body, j = collect_body(lines, i, ind)
                    tabs.append((html.unescape(m2["label"]), body))
                    i = j
                    k = i
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    m3 = TAB_RE.match(lines[k]) if k < len(lines) else None
                    if m3 and len(m3["ind"]) == ind:
                        i = k
                    else:
                        break
                out += self.render_tabs(tabs, container_indent(out, ind))
                continue
            m = ADM_RE.match(ln)
            if m:
                ind = len(m["ind"])
                body, j = collect_body(lines, i, ind)
                out += self.render_adm(m, body, container_indent(out, ind))
                i = j
                continue
            # Card grids put a "---" rule inside each list item. Drop it for print.
            if DIV_LINE_RE.match(ln) or re.match(r"^ {2,}---\s*$", ln):
                out.append("")
                i += 1
                continue
            v = IFRAME_RE.search(ln)
            if v:
                out += ["", " " * indent_of(ln) + f"*Video:* <{video_url(v.group(1))}>", ""]
                i += 1
                continue
            ln = self.prose(ln)
            # "-    text" puts the content at column 5 (often after an icon is
            # removed). MkDocs continues the item at column 4, pandoc at column 5.
            # Move the content to column 4.
            lm = LIST_WIDE_RE.match(ln)
            if lm and len(lm.group(2)) + len(lm.group(3)) > 4:
                ln = lm.group(1) + lm.group(2) + " " * max(1, 4 - len(lm.group(2))) + lm.group(4)
            out.append(ln)
            i += 1
        return out

    # ---- inline pass (prose lines only) ------------------------------------
    def prose(self, line):
        # Protect inline code spans.
        parts = re.split(r"(`+[^`]*?`+)", line)
        for k in range(0, len(parts), 2):
            parts[k] = self.prose_segment(parts[k])
        return "".join(parts)

    def prose_segment(self, s):
        s = re.sub(r"<br\s*/?>", " ", s)
        s = re.sub(r":(?:material|octicons|fontawesome|simple)-[\w-]+:", "", s)
        s = re.sub(r"\{:[^}\n]*\}", "", s)
        s = re.sub(r"\{\s*\.(?!adksupport|tablabel|nopython)[\w-]+[^}\n]*\}", "", s)
        s = re.sub(r'<img\s[^>]*?src="([^"]+)"[^>]*?(?:alt="([^"]*)")?[^>]*>',
                   lambda m: f"![{m.group(2) or ''}]({m.group(1)})", s)
        s = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)(\{[^}\n]*\})?', self.image, s)
        s = re.sub(r"(?<!!)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", self.link, s)
        s = re.sub(r"^(\s*\[[^\]]+\]:\s*)(\S+)", lambda m: m.group(1) + (self.resolve_link(m.group(2)) or "#"), s)
        return emoji_map(s)

    def link(self, m):
        text, url = m.group(1), m.group(2)
        target = self.resolve_link(url)
        if target is None:
            return text
        return f"[{text}]({target})"

    def resolve_link(self, url):
        if url.startswith(("mailto:", "tel:")):
            return url
        if url.startswith(("http://", "https://")):
            if url.startswith(SITE):
                p = self.ctx.find_page(url[len(SITE):].split("#")[0])
                if p:
                    return self.ctx.ref_target(p, self)
            return url
        if url.startswith("#"):
            return None
        path = url.split("#")[0].split("?")[0]
        if not path:
            return None
        if path.startswith("/"):
            p = path.lstrip("/")
            if p.startswith("adk-docs/"):
                p = p[len("adk-docs/"):]
        else:
            p = posixpath.normpath(posixpath.join(self.dir, path))
        page = self.ctx.find_page(p)
        if page:
            return self.ctx.ref_target(page, self)
        return SITE + web_path(p)

    def image(self, m):
        alt, src = m.group(1), m.group(2)
        path = self.ctx.image(src, self)
        if not path:
            return f"*[Image: {alt or src}]*"
        rel, width = path
        return f"![{alt}]({rel}){{width={width:.2f}in}}"


def md_escape_label(s):
    return s.replace("[", "(").replace("]", ")")


def web_path(p):
    p = re.sub(r"(^|/)index\.md$", r"\1", p)
    p = re.sub(r"\.md$", "/", p)
    return p


EMOJI = {
    "✅": "Yes", "❌": "No", "✔": "Yes", "✖": "No", "⚠️": "Warning:",
    "⚠": "Warning:", "▶": ">", "◀": "<", "▼": "v", "❗": "!",
}
EMOJI_STRIP = re.compile("[\U0001F000-\U0001FAFF️‍]")


def emoji_map(s):
    for k, v in EMOJI.items():
        s = s.replace(k, v)
    return EMOJI_STRIP.sub("", s)


# --------------------------------------------------------------------------
# Whole-page passes
# --------------------------------------------------------------------------
SUPPORT_RE = re.compile(
    r'^(?P<ind>[ \t]*)<div class="language-support-tag"[^>]*>(?P<body>.*?)</div>[ \t]*$', re.M | re.S)
TABLE_RE = re.compile(r"^[ \t]*<table.*?</table>[ \t]*$", re.M | re.S)


def support_tag(m):
    spans = dict()
    for cls, txt in re.findall(r'<span class="lst-([\w-]+)"[^>]*>([^<]*)</span>', m["body"]):
        spans.setdefault(cls, []).append(txt.strip())
    py = spans.get("python")
    extra = ", ".join(spans.get("preview", []))
    if py:
        text = "Supported in ADK " + py[0] + (f" ({extra})" if extra else "")
    else:
        others = [v[0] for k, v in spans.items() if k not in ("supported", "preview")]
        text = "Not available in ADK Python" + (f" (only {', '.join(others)})" if others else "")
    return f'\n{m["ind"]}[{text}]{{.adksupport}}\n'


def html_table_to_md(m):
    r = subprocess.run([PANDOC, "-f", "html", "-t", "markdown-simple_tables-multiline_tables-raw_html"],
                       input=m.group(0), capture_output=True, text=True, encoding="utf-8")
    return "\n" + r.stdout + "\n"


def strip_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[text.find("\n", end + 1) + 1:]
    return text


def fix_headings(lines, slug, fallback_title):
    """Shift headings so the page title is H1 (a chapter) and add its label."""
    in_code = False
    fence = None
    heads = []
    for idx, ln in enumerate(lines):
        c = re.match(r"^[ \t]*(`{3,}|~{3,})", ln)
        if c:
            if not in_code:
                in_code, fence = True, c.group(1)
            elif c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence):
                in_code = False
            continue
        if in_code:
            continue
        m = HEADING_RE.match(ln)
        if m:
            heads.append((idx, len(m.group(1)), m.group(2)))
    if not heads:
        return [f"# {fallback_title} {{#{slug}}}", ""] + lines, fallback_title
    top = min(h[1] for h in heads)
    first_idx, _, first_text = heads[0]
    first_text = re.sub(r"\s*\{[^}]*\}\s*$", "", first_text)
    many_top = sum(1 for h in heads if h[1] == top) > 1 or heads[0][1] != top
    for idx, level, text in heads:
        if idx == first_idx:
            lines[idx] = f"# {first_text} {{#{slug}}}"
        else:
            new = level - top + 1 + (1 if many_top else 0)
            new = max(2, min(new, 6))
            lines[idx] = "#" * new + " " + text
    return lines, strip_md(first_text)


OTHER_LANG_HEADING = re.compile(r"\b(TypeScript|JavaScript|Java|Kotlin|Go(?! [a-z]))\b")


def drop_other_language_sections(lines, path):
    """Remove sections (H2 and below) whose heading names only another SDK
    language, for example "ADK Go 1.x compatibility"."""
    out, drop_level, in_code, fence = [], None, False, None
    for ln in lines:
        c = re.match(r"^[ \t]*(`{3,}|~{3,})", ln)
        if c:
            if not in_code:
                in_code, fence = True, c.group(1)
            elif c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence):
                in_code = False
        m = None if in_code or c else HEADING_RE.match(ln)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if drop_level is not None and level <= drop_level:
                drop_level = None
            if (drop_level is None and level >= 2 and OTHER_LANG_HEADING.search(text)
                    and "Python" not in text):
                drop_level = level
                STATS["sections_dropped"] = STATS.get("sections_dropped", 0) + 1
                warn(f"{path}: dropped section '{text}'")
        if drop_level is None:
            out.append(ln)
    return out


def strip_md(s):
    s = re.sub(r"[`*_]", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return s.strip()


# --------------------------------------------------------------------------
# Book context
# --------------------------------------------------------------------------
class Book:
    def __init__(self, plan, nav_titles):
        self.plan = plan
        self.nav_titles = nav_titles
        self.pages = {}         # path -> dict(vol, chapter, slug, step)
        self.images = {}
        n = 0
        steps = {s["number"]: s for s in plan["steps"]}
        for vol in plan["volumes"]:
            for sn in vol["steps"]:
                for p in steps[sn]["pages"]:
                    n += 1
                    self.pages[p] = dict(vol=vol["number"], chapter=n, step=sn,
                                         slug="pg-" + re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-"))

    def find_page(self, p):
        p = p.strip("/")
        for cand in (p, p + ".md", posixpath.join(p, "index.md"), p.replace(".html", ".md")):
            cand = posixpath.normpath(cand) if cand else cand
            if cand in self.pages:
                return cand
        return None

    def ref_target(self, target, from_page):
        info = self.pages[target]
        mine = self.pages[from_page.path]["vol"]
        if info["vol"] == mine:
            return "#" + info["slug"]
        return f"xref:vol{info['vol']}:ch{info['chapter']}"

    def image(self, src, page):
        if src.startswith(("http://", "https://")):
            key = "url:" + src
        else:
            p = src.split("#")[0].split("?")[0]
            p = p.lstrip("/") if p.startswith("/") else posixpath.normpath(posixpath.join(page.dir, p))
            if p.startswith("adk-docs/"):
                p = p[len("adk-docs/"):]
            key = p
        if key in self.images:
            return self.images[key]
        result = self._convert_image(key, src, page)
        self.images[key] = result
        return result

    def _convert_image(self, key, src, page):
        h = hashlib.sha1(key.encode()).hexdigest()[:12]
        if key.startswith("url:"):
            ext = Path(src.split("?")[0]).suffix.lower() or ".png"
            raw = IMG / f"dl-{h}{ext}"
            if not raw.exists():
                try:
                    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
                    raw.write_bytes(urllib.request.urlopen(req, timeout=30).read())
                except Exception as e:  # noqa: BLE001
                    warn(f"{page.path}: image download failed {src}: {e}")
                    return None
            f = raw
        else:
            f = DOCS / key
            if not f.exists():
                warn(f"{page.path}: missing image {src}")
                return None
        ext = f.suffix.lower()
        STATS["images"] += 1
        if ext == ".svg":
            dst = IMG / f"{h}.png"
            if not dst.exists():
                render_svgs([{"src": str(f), "dst": str(dst)}])
            if not dst.exists():
                warn(f"{page.path}: svg render failed {src}")
                return None
            w_css = Image.open(dst).width / 3
            return f"img/{dst.name}", min(w_css / 96, TEXT_WIDTH_IN)
        dst = IMG / f"{h}.png" if ext in (".gif", ".webp") else IMG / f"{h}{ext}"
        if not dst.exists():
            try:
                im = Image.open(f)
                if ext in (".gif", ".webp"):
                    im.seek(0)
                    im.convert("RGB").save(dst)
                else:
                    shutil.copyfile(f, dst)
            except Exception as e:  # noqa: BLE001
                warn(f"{page.path}: image convert failed {src}: {e}")
                return None
        w = Image.open(dst).width
        # Screenshots are mostly 2x. Treat 150 px as 1 inch. Cap at 4.8 in so that
        # dark UI screenshots do not fill a page with toner.
        return f"img/{dst.name}", min(max(w / 150, 1.5), RASTER_MAX_IN)


def render_svgs(jobs):
    jf = IMG / "svg-jobs.json"
    jf.write_text(json.dumps(jobs), encoding="utf-8")
    r = subprocess.run(["node", str(TOOLS / "svg2png.js"), str(jf)], capture_output=True, text=True, cwd=TOOLS,
                       env={**os.environ, "CHROME_PATH": find_chrome()})
    if r.returncode != 0:
        warn("svg render: " + r.stderr[-400:])


def find_chrome():
    for c in [os.environ.get("CHROME_PATH")] + CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    sys.exit("Chrome or Chromium not found. Set CHROME_PATH to the browser executable.")


def ensure_source(pull):
    if not REPO.exists():
        REPO.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", DOCS_GIT, str(REPO)], check=True)
    elif pull:
        subprocess.run(["git", "-C", str(REPO), "pull", "--depth", "1", "--ff-only"], check=True)


def nav_title_map():
    class L(yaml.SafeLoader):
        pass
    L.add_multi_constructor("", lambda l, s, n: None)
    cfg = yaml.load((REPO / "mkdocs.yml").read_text(encoding="utf-8"), Loader=L)
    titles = {}

    def walk(node, label=None):
        if isinstance(node, list):
            for x in node:
                walk(x, label)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, str) and node.endswith(".md"):
            titles.setdefault(node, label or node)
    walk(cfg["nav"])
    return titles


def process_page(book, path):
    page = Page(path, book)
    text = (DOCS / path).read_text(encoding="utf-8").replace("\r\n", "\n").expandtabs(4)
    text = strip_frontmatter(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = expand_snippets(text.split("\n"), path)
    text = "\n".join(lines)
    text = SUPPORT_RE.sub(support_tag, text)
    if "Not available in ADK Python" in "\n".join(text.split("\n")[:12]):
        warn(f"{path}: the whole page is not available in ADK Python; remove it from volumes.yml")
    text = TABLE_RE.sub(html_table_to_md, text)
    lines = page.transform(text.split("\n"))
    info = book.pages[path]
    lines, title = fix_headings(lines, info["slug"], book.nav_titles.get(path, path))
    lines = drop_other_language_sections(lines, path)
    info["title"] = title
    body = "\n".join(lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


# --------------------------------------------------------------------------
# LaTeX front matter and step pages
# --------------------------------------------------------------------------
def tex_escape(s):
    rep = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
           "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(rep.get(c, c) for c in s)


def url_tex(u):
    return r"\url{" + u.replace("%", r"\%").replace("#", r"\#") + "}"


def step_opener(step, book):
    label = "Appendix" if step.get("appendix") else f"Step {step['number']}"
    rows = []
    for p in step["pages"]:
        info = book.pages[p]
        rows.append(r"\stepchapter{%d}{%s}{%s}" % (info["chapter"], tex_escape(info["title"]), info["slug"]))
    todo = "\n".join(r"\stepdo{%s}{%s}{%s}" % (tex_escape(d["name"]), tex_escape(d["where"] + ", " + d["time"]),
                                                url_tex(d["url"]) if d["url"] else "")
                     for d in step.get("do", []))
    after = (r"\par\bigskip{\sffamily\bfseries Then do}\par\medskip" + "\n" + todo) if todo else \
        r"\par\bigskip{\sffamily\itshape Optional. Read this part when you need live or voice agents.}"
    title = step["title"].replace("Appendix: ", "")
    return "\n".join([
        "", "```{=latex}",
        r"\stepopener{%s}{%s}" % (label, tex_escape(title)),
        r"{\sffamily\bfseries Read these chapters}\par\medskip",
        *rows,
        after,
        r"\clearpage",
        "```", ""])


def checkpoint(step, next_step):
    if not step.get("do"):
        return ""
    items = "\n".join(r"\checkitem{%s}{%s}{%s}" % (tex_escape(d["name"]), tex_escape(d["where"] + ", " + d["time"]),
                                                   url_tex(d["url"]) if d["url"] else "")
                      for d in step["do"])
    nxt = (f"Do this work before you start Step {next_step}." if next_step
           else "This is the last step of the manual.")
    return "\n".join([
        "", "```{=latex}",
        r"\begin{checkpoint}{%d}{%s}{%s}" % (step["number"], tex_escape(step["title"]), nxt),
        items,
        r"\end{checkpoint}",
        "```", ""])


def schedule_table(plan, book, vol_no):
    steps = {s["number"]: s for s in plan["steps"]}
    rows = []
    for vol in plan["volumes"]:
        for sn in vol["steps"]:
            s = steps[sn]
            chs = [book.pages[p]["chapter"] for p in s["pages"]]
            read = f"Vol.~{vol['number']}, ch.~{chs[0]}--{chs[-1]}"
            do = r" \newline ".join(r"\textbullet~" + tex_escape(d["name"]) + r" \emph{(" + tex_escape(d["time"]) + ")}"
                                    for d in s.get("do", [])) or r"\emph{Optional reading}"
            name = "App." if s.get("appendix") else str(sn)
            title = tex_escape(s["title"].replace("Appendix: ", ""))
            if vol["number"] == vol_no:
                title = r"\textbf{" + title + "}"
            rows.append(f"{name} & {title} \\newline {{\\footnotesize {read}}} & {do} & \\checkbox \\\\ \\midrule")
    return "\n".join(rows)


def front_matter(plan, book, vol, commit, commit_date):
    b = plan["book"]
    steps = vol["steps"]
    by_no = {s["number"]: s for s in plan["steps"]}
    real = [s for s in steps if not by_no[s].get("appendix")]
    step_range = f"Steps {real[0]}--{real[-1]} of 8" + (" and an appendix" if len(real) < len(steps) else "")
    return rf"""
\begin{{titlepage}}
\centering\sffamily
\vspace*{{1.4in}}
{{\Huge\bfseries {tex_escape(b['title'])}\par}}
\vspace{{0.25in}}
{{\Large {tex_escape(b['subtitle'])}\par}}
\vspace{{1.1in}}
{{\LARGE Volume {vol['number']}\par}}
\vspace{{0.12in}}
{{\huge\bfseries {tex_escape(vol['title'])}\par}}
\vspace{{0.15in}}
{{\large {step_range}\par}}
\vfill
{{\small Unofficial edition. Not affiliated with or endorsed by Google.\par
Source: {url_tex(b['source_url'])}\par
Docs commit {commit[:7]} of {commit_date}\par}}
\end{{titlepage}}

\thispagestyle{{empty}}
\vspace*{{\fill}}
{{\small\sffamily
\textbf{{About this edition}}\par\smallskip
This book is a print conversion of the Agent Development Kit (ADK) documentation.
The text and the code come from the {url_tex(b['repo_url'])} repository, commit {tex_escape(commit[:12])}
({commit_date}). Google publishes that content under the Apache License 2.0. The full license text is at the end of this volume.
This edition is not a Google publication.\par\smallskip
Changes from the source: the conversion keeps only the Python version of each code example. Pages for other languages,
the API reference, the integrations catalog, and the community pages are not included. Read them online at {url_tex(b['source_url'])}.\par\smallskip
A cross-reference such as ``(p.~42)'' points to a page in this volume. A reference such as ``(Vol.~2, ch.~31)'' points to another volume.\par\smallskip
Get the newest PDFs and the build scripts at {url_tex(b['project_url'])}.\par}}

\cleardoublepage
\chapter*{{How to use this manual}}
\markboth{{How to use this manual}}{{}}
Each step has two parts. First, read the chapters for the step. Then, do the course work for the step.
Do the course work before you start the next step. Each step ends with a checkpoint page.
Use the checkpoint page to record the date and your notes.\par\medskip
Do one step each week. Then you complete the manual and the courses in about eight weeks.
Most courses are in the Google Skills learning path ``Develop Agents with Agent Development Kit (ADK)'':
{url_tex('https://www.skills.google/paths/3545')}. Step~8 uses the path ``Deploy Production Ready Agents'':
{url_tex('https://www.skills.google/paths/3802')}.\par\medskip
The courses can use older names than this manual. For example, a lab can say ``Vertex AI Agent Engine''
where this manual says ``Agent Runtime''.\par\bigskip
{{\small
\begin{{tabularx}}{{\linewidth}}{{@{{}}p{{0.35in}}p{{1.85in}}X >{{\centering\arraybackslash}}p{{0.42in}}@{{}}}}
\toprule
\sffamily\bfseries Step & \sffamily\bfseries Read & \sffamily\bfseries Then do & \sffamily\bfseries Done \\ \midrule
{schedule_table(plan, book, vol['number'])}
\end{{tabularx}}}}

\cleardoublepage
{{\hypersetup{{linkcolor=black}}\tableofcontents}}
"""


def volume_markdown(plan, book, vol, bodies):
    steps = {s["number"]: s for s in plan["steps"]}
    first_ch = min(book.pages[p]["chapter"] for sn in vol["steps"] for p in steps[sn]["pages"])
    parts = ["```{=latex}", r"\setcounter{chapter}{%d}" % (first_ch - 1), "```", ""]
    real_steps = [s["number"] for s in plan["steps"] if not s.get("appendix")]
    for sn in vol["steps"]:
        s = steps[sn]
        parts.append(step_opener(s, book))
        for p in s["pages"]:
            parts.append(bodies[p])
            parts.append("")
        nxt = sn + 1 if sn + 1 in real_steps else None
        parts.append(checkpoint(s, nxt))
    # Apache 2.0 section 4(a): give each reader a copy of the license.
    parts += ["", "```{=latex}",
              r"\cleardoublepage\phantomsection\addcontentsline{toc}{part}{Apache License 2.0}",
              r"\chapter*{Apache License 2.0}\markboth{Apache License 2.0}{}",
              r"The ADK documentation in this volume is under the following license.\par\medskip",
              r"\VerbatimInput[breaklines,fontsize=\scriptsize]{LICENSE-adk-docs.txt}",
              "```", ""]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def write_release_notes(plan, built, commit, commit_date):
    rows = "\n".join(f"| {v['number']} | {v['title']} | {pages} | `{name}` |" for v, pages, name in built)
    total = sum(int(p) for _, p, _ in built if str(p).isdigit())
    (DIST / "release-notes.md").write_text(
        f"PDFs built from [google/adk-docs@{commit[:7]}]({plan['book']['repo_url']}/commit/{commit}) "
        f"({commit_date}).\n\n| Vol. | Title | Pages | File |\n|---|---|---|---|\n{rows}\n\n"
        f"Total: {total} pages. Letter size, for duplex printing.\n\n"
        "Unofficial edition. Not affiliated with or endorsed by Google. "
        "The ADK documentation is under the Apache License 2.0.\n", encoding="utf-8")


def run(cmd, cwd, what):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"--- {what} failed (exit {r.returncode}) ---")
        out = r.stdout or ""
        i = out.find("\n!")
        print(out[i:i + 2500] if i != -1 else out[-2500:])
        print((r.stderr or "")[-2000:])
        sys.exit(1)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", type=int, action="append")
    ap.add_argument("--tex-only", action="store_true")
    ap.add_argument("--pull", action="store_true", help="git pull src/adk-docs before the build")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    ensure_source(args.pull)
    shutil.copyfile(REPO / "LICENSE", OUT / "LICENSE-adk-docs.txt")
    (OUT / "puppeteer.json").write_text(
        json.dumps({"executablePath": find_chrome(), "args": ["--no-sandbox"]}), encoding="utf-8")

    langs = subprocess.run([PANDOC, "--list-highlight-languages"], capture_output=True, text=True).stdout.split()
    KNOWN_LANGS.update(langs)

    plan = yaml.safe_load((ROOT / "volumes.yml").read_text(encoding="utf-8"))
    commit, commit_date = subprocess.run(["git", "log", "-1", "--format=%H %cs"], cwd=REPO,
                                         capture_output=True, text=True).stdout.split()
    book = Book(plan, nav_title_map())

    # Pass 1: every page (titles are needed by all volumes for cross-references).
    bodies = {p: process_page(book, p) for p in book.pages}
    # Cross-volume references: xref:volN:chM -> plain text marker.
    for p, body in bodies.items():
        bodies[p] = re.sub(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\(xref:vol(\d+):ch(\d+)\)",
                           r"\1 (Vol.\\ \2, ch.\\ \3)", body)

    vols = [v for v in plan["volumes"] if not args.vol or v["number"] in args.vol]
    built = []
    for vol in vols:
        stem = f"vol{vol['number']}"
        md = OUT / f"{stem}.md"
        md.write_text(volume_markdown(plan, book, vol, bodies), encoding="utf-8")
        (OUT / f"{stem}-front.tex").write_text(front_matter(plan, book, vol, commit, commit_date), encoding="utf-8")
        (OUT / f"{stem}-vars.tex").write_text(
            "\\newcommand{\\voltitle}{Vol.~%d: %s}\n\\newcommand{\\buildstamp}{ADK Print Edition, Vol.~%d \\textperiodcentered{} docs %s (%s)}\n"
            % (vol["number"], tex_escape(vol["title"]), vol["number"], commit[:7], commit_date), encoding="utf-8")
        exts = ("markdown+lists_without_preceding_blankline-blank_before_header-blank_before_blockquote"
                "-tex_math_dollars-tex_math_single_backslash-tex_math_double_backslash-raw_tex-citations"
                "-subscript-superscript-example_lists-yaml_metadata_block")
        cmd = [PANDOC, md.name, "-f", exts, "-t", "latex", "-s", "-o", f"{stem}.tex",
               "--lua-filter", str(BUILD / "filters.lua"),
               "-H", str(BUILD / "preamble.tex"), "-H", f"{stem}-vars.tex",
               "-B", f"{stem}-front.tex",
               "--syntax-highlighting=monochrome", "--top-level-division=chapter", "--number-sections",
               "-V", "documentclass=book", "-V", "classoption=openany", "-V", "classoption=twoside",
               "-V", "fontsize=11pt", "-V", "papersize=letter",
               "-V", "geometry=inner=1.3in,outer=1.2in,top=1in,bottom=1in,headsep=0.3in,footskip=0.45in",
               "-V", "colorlinks=true", "-V", "linkcolor=linkblue",
               "-V", "urlcolor=linkblue", "-V", "toccolor=black", "-V", "secnumdepth=1",
               "-V", "subparagraph=true"]
        run(cmd, OUT, f"pandoc {stem}")
        print(f"{stem}: wrote {stem}.tex")
        if args.tex_only:
            continue
        for n in range(3):
            r = run([XELATEX, "-interaction=nonstopmode", "-halt-on-error", f"{stem}.tex"], OUT,
                    f"xelatex {stem} pass {n + 1}")
        m = re.search(r"Output written on .*?\((\d+) pages", r.stdout)
        pages = m.group(1) if m else "?"
        dst = DIST / f"{vol['file']}.pdf"
        shutil.copyfile(OUT / f"{stem}.pdf", dst)
        print(f"{stem}: {pages} pages -> {dst}")
        built.append((vol, pages, dst.name))

    if built and not args.tex_only:
        write_release_notes(plan, built, commit, commit_date)
    print("stats:", STATS)
    if WARNINGS:
        print(f"{len(WARNINGS)} warnings:")
        for w in WARNINGS[:80]:
            print("  -", w)


if __name__ == "__main__":
    main()
