"""Render PDF pages to PNG for a visual check: peek.py file.pdf 1 5 9-12"""
import sys, pathlib
import pymupdf as fitz
pdf = fitz.open(sys.argv[1])
out = pathlib.Path(__file__).parent / "out" / "peek"
out.mkdir(parents=True, exist_ok=True)
pages = []
for a in sys.argv[2:]:
    if "-" in a:
        s, e = map(int, a.split("-")); pages += range(s, e + 1)
    else:
        pages.append(int(a))
for p in pages:
    pix = pdf[p - 1].get_pixmap(dpi=int(__import__("os").environ.get("DPI","80")))
    f = out / f"p{p:03d}.png"; pix.save(f); print(f)
print("total pages", len(pdf))
