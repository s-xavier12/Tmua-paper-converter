"""Generate a small, realistic TMUA-style PDF for tests.

The PDF is printed by headless Chromium from HTML with KaTeX-rendered maths
and inline SVG diagrams, so it has a messy real-world text layer (maths
glyphs split into spans) and vector graphics, like typical exam PDFs.

Run directly to regenerate ``tests/data/sample_paper.pdf``:

    python tests/make_sample_paper.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KATEX_DIR = HERE.parent / "tmua_converter" / "web" / "static" / "vendor" / "katex"
OUT = HERE / "data" / "sample_paper.pdf"

# The "ground truth" transcription of the sample paper, used by tests and by
# the fake Claude client.  Page numbers are 1-based PDF pages.
EXPECTED = {
    "title": "Sample Mathematics Admissions Paper",
    "paper": "Paper 1",
    "durationMinutes": 40,
    "questions": [
        {
            "number": 1,
            "page": 2,
            "stem": "Find the complete set of values of $x$ for which\n\n$$\\frac{x^2 - 4}{x + 1} \\le 0$$",
            "options": ["$x \\le -2$", "$-2 \\le x < -1$ or $x \\ge 2$", "$x \\le -2$ or $-1 < x \\le 2$",
                        "$-1 < x \\le 2$", "$x \\ge 2$"],
        },
        {
            "number": 2,
            "page": 2,
            "stem": "The diagram shows triangle $ABC$ with $AB = 9/\\sqrt{2}$ and angle $BAC = 45^\\circ$.\n\nWhat is the area of triangle $ABC$?",
            "options": ["$\\frac{81}{4}$", "$\\frac{81}{2}$", "$\\frac{81\\sqrt{2}}{4}$", "$81$"],
            "figure": "triangle",
        },
        {
            "number": 3,
            "page": 3,
            "stem": "Which one of the following graphs could be the graph of $y = x^3 - x$?",
            "options": ["Graph A", "Graph B", "Graph C", "Graph D"],
            "figure": "graph_options",
        },
        {
            "number": 4,
            "page": 5,
            "stem": "Evaluate\n\n$$\\sum_{k=1}^{10} \\log_{2}\\left(\\frac{k+1}{k}\\right)$$",
            "options": ["$\\log_{2} 11$", "$\\log_{2} 10$", "$1 + \\log_{2} 11$", "$11$", "$\\log_{10} 2$"],
        },
    ],
}


def _page(inner: str) -> str:
    return f'<section class="page">{inner}<div class="footer">© Sample Board 2026</div></section>'


def _q(number: int, body: str) -> str:
    return f'<div class="q"><div class="qn">{number}</div><div class="qb">{body}</div></div>'


def _opts(opts: list[str]) -> str:
    rows = "".join(f'<div class="opt"><b>{chr(65 + i)}</b><span>{o}</span></div>' for i, o in enumerate(opts))
    return f'<div class="opts">{rows}</div>'


def _tex(s: str) -> str:
    """Keep TeX for KaTeX auto-render; escape HTML-special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


TRIANGLE_SVG = """
<svg width="260" height="170" viewBox="0 0 260 170" xmlns="http://www.w3.org/2000/svg">
  <polygon points="20,150 240,150 120,30" fill="none" stroke="black" stroke-width="1.5"/>
  <path d="M 45 150 A 25 25 0 0 0 38 132" fill="none" stroke="black"/>
  <text x="5" y="165" font-size="14" font-style="italic">A</text>
  <text x="243" y="165" font-size="14" font-style="italic">B</text>
  <text x="115" y="22" font-size="14" font-style="italic">C</text>
  <text x="52" y="143" font-size="11">45°</text>
</svg>
"""


def _graph(label: str, path: str) -> str:
    return f"""
<div class="g"><svg width="150" height="120" viewBox="0 0 150 120" xmlns="http://www.w3.org/2000/svg">
  <line x1="5" y1="60" x2="145" y2="60" stroke="black"/>
  <line x1="75" y1="5" x2="75" y2="115" stroke="black"/>
  <path d="{path}" fill="none" stroke="black" stroke-width="1.5"/>
  <text x="138" y="74" font-size="11" font-style="italic">x</text>
  <text x="80" y="14" font-size="11" font-style="italic">y</text>
</svg><div class="gl">{label}</div></div>"""


GRAPHS = (
    '<div class="graphs">'
    + _graph("A", "M 20 110 C 45 -20, 60 20, 75 60 S 105 140, 130 10")
    + _graph("B", "M 20 10 C 45 140, 60 100, 75 60 S 105 -20, 130 110")
    + _graph("C", "M 20 110 Q 75 -40 130 110")
    + _graph("D", "M 20 60 L 130 60")
    + "</div>"
)


def build_html() -> str:
    qs = EXPECTED["questions"]
    cover = _page(
        '<h1>Sample Mathematics Admissions Paper</h1><h2>Paper 1</h2>'
        '<p class="big">Time allowed: 40 minutes</p>'
        "<p>There are four questions in this paper. Attempt all of them.</p>"
        "<p>Calculators and formula booklets are NOT permitted.</p>"
    )
    q1 = qs[0]
    q2 = qs[1]
    p2 = _page(
        _q(1, "<p>Find the complete set of values of $x$ for which</p>"
              "<p>$$" + _tex("\\frac{x^2 - 4}{x + 1} \\le 0") + "$$</p>" + _opts([_tex(o) for o in q1["options"]]))
        + _q(2, "<p>The diagram shows triangle $ABC$ with $AB = 9/\\sqrt{2}$ and angle $BAC = 45^\\circ$.</p>"
                f'<div class="fig">{TRIANGLE_SVG}</div>'
                "<p>What is the area of triangle $ABC$?</p>" + _opts(q2["options"]))
    )
    p3 = _page(_q(3, "<p>Which one of the following graphs could be the graph of $y = x^3 - x$?</p>" + GRAPHS
                  + _opts(qs[2]["options"])))
    blank = _page('<p class="blank">BLANK PAGE</p>')
    p5 = _page(_q(4, "<p>Evaluate</p><p>$$" + _tex("\\sum_{k=1}^{10} \\log_{2}\\left(\\frac{k+1}{k}\\right)") + "$$</p>"
                  + _opts([_tex(o) for o in qs[3]["options"]])))
    css = """
      @page { size: A4; margin: 0; }
      body { margin: 0; font-family: 'DejaVu Serif', serif; font-size: 12pt; }
      .page { width: 210mm; height: 297mm; box-sizing: border-box; padding: 22mm 20mm; position: relative;
              page-break-after: always; overflow: hidden; }
      .footer { position: absolute; bottom: 12mm; left: 0; right: 0; text-align: center; font-size: 8pt; }
      h1 { font-size: 20pt; margin-top: 40mm; text-align: center; } h2 { text-align: center; }
      .big { font-size: 14pt; text-align: center; font-weight: bold; }
      .q { display: flex; margin-bottom: 18mm; } .qn { width: 12mm; font-weight: bold; font-family: sans-serif; }
      .qb { flex: 1; } .opts { margin-top: 6mm; } .opt { display: flex; margin: 2.5mm 0; }
      .opt b { width: 10mm; font-family: sans-serif; }
      .fig { margin: 4mm 0 4mm 20mm; } .graphs { display: grid; grid-template-columns: 170px 170px; gap: 6mm 14mm;
              margin: 6mm 0 2mm 10mm; } .g { text-align: center; } .gl { font-weight: bold; font-family: sans-serif; }
      .blank { text-align: center; margin-top: 120mm; font-weight: bold; }
    """
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{(KATEX_DIR / 'katex.min.css').as_uri()}">
<script src="{(KATEX_DIR / 'katex.min.js').as_uri()}"></script>
<script src="{(KATEX_DIR / 'contrib' / 'auto-render.min.js').as_uri()}"></script>
<style>{css}</style></head><body>
{cover}{p2}{p3}{blank}{p5}
<script>renderMathInElement(document.body, {{delimiters: [{{left: '$$', right: '$$', display: true}},
  {{left: '$', right: '$', display: false}}], throwOnError: true}}); document.body.dataset.ready = '1';</script>
</body></html>"""


def build(out: Path = OUT) -> Path:
    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(HERE.parent))
    from tmua_converter.browser import launch_chromium

    out.parent.mkdir(parents=True, exist_ok=True)
    html_path = out.with_suffix(".html")
    html_path.write_text(build_html(), encoding="utf-8")
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_selector("body[data-ready='1']")
        page.evaluate("document.fonts.ready")
        page.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
        browser.close()
    html_path.unlink()
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
