"""Build realistic test papers with pdflatex, plus their exact expected transcription.

Real exam papers are typeset like this: LaTeX fonts, fraction bars and root
bars drawn as lines, big brackets and sums from the maths extension font,
diagrams drawn in picture mode or included as vector PDFs.

The question content below is written ONCE, in the .tmua.json convention, and
used both to typeset the PDF and as the expected output - so the tests check
the converter against a known-correct answer.

    python tests/make_latex_papers.py      # needs pdflatex + xelatex (TMUA_PDFLATEX / TMUA_XELATEX)

The generated PDFs are committed, so running the tests does not need TeX.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

TITLE = "Sample Admissions Test Paper 1"
META = {"paper": "Paper 1", "year": "2026", "durationMinutes": 75}

QUESTIONS = [
    {
        "stem": "Find the complete set of values of $x$ for which\n\n$$\\frac{x^2 - 4}{x + 1} \\le 0$$",
        "options": ["$x \\le -2$", "$-2 \\le x < -1$ or $x \\ge 2$", "$x \\le -2$ or $-1 < x \\le 2$",
                    "$-1 < x \\le 2$", "$x \\ge 2$"],
    },
    {
        "stem": "The diagram shows triangle $ABC$ with $AB = 9/\\sqrt{2}$ and angle $BAC = 45^\\circ$.\n\n"
                "What is the area of triangle $ABC$?",
        "figure_after_paragraph": 0,
        "figure": "triangle",
        "options": ["$\\frac{81}{4}$", "$\\frac{81}{2}$", "$\\frac{81\\sqrt{2}}{4}$", "$81$"],
    },
    {
        "stem": "Which one of the following could be the graph of $y = x^3 - x$?",
        "figure": "graphs",
        "options": ["Graph A", "Graph B", "Graph C", "Graph D"],
    },
    {
        "stem": "Evaluate\n\n$$\\sum_{k=1}^{10} \\log_2 \\left(\\frac{k + 1}{k}\\right)$$",
        "options": ["$\\log_2 11$", "$\\log_2 10$", "$1 + \\log_2 11$", "$11$", "$\\log_{10} 2$"],
    },
    {
        "stem": "Consider the following statements about the function $f(x) = |x - 1| - 2$:\n\n"
                "I $f(-1) = 0$\n\nII $f(x) \\ge -2$ for all real $x$\n\n"
                "III the equation $f(x) = 1$ has exactly two solutions\n\n"
                "Which of these statements is/are true?",
        "options": ["none of them", "I only", "II only", "I and II only", "I, II and III"],
    },
    {
        "stem": "Given that $\\int_0^a (3x^2 - 2x) dx = 4$ where $a > 0$, find the value of $a$.",
        "options": ["$1$", "$2$", "$\\frac{1 + \\sqrt{17}}{2}$", "$\\sqrt{3}$", "$4$"],
    },
    {
        "stem": "The sequence $u_n$ is defined by $u_1 = 2$ and $u_{n+1} = \\frac{u_n}{1 + u_n}$ for $n \\ge 1$.\n\n"
                "What is the value of $u_{10}$?",
        "page_break_before_options": True,
        "options": ["$\\frac{2}{19}$", "$\\frac{1}{10}$", "$\\frac{2}{21}$", "$2^{-10}$", "$\\frac{1}{2^9}$"],
    },
    {
        "stem": "The table shows some values of the function $f$.\n\nWhich one of the following could be $f(x)$?",
        "figure_after_paragraph": 0,
        "figure": "table",
        "options": ["$2x + 1$", "$x^2 + 1$", "$3^x$", "$\\sqrt{x + 1}$", "$\\frac{6}{x + 1}$"],
    },
]


# --------------------------------------------------------------------------- figures (vector PDFs)
def make_graph_pdf(path: Path, kind: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=110, height=80)
    sh = page.new_shape()
    sh.draw_line((5, 40), (105, 40))
    sh.draw_line((55, 3), (55, 77))
    sh.finish(color=(0, 0, 0), width=0.7, closePath=False)
    if kind == "cubic":
        sh.draw_bezier((12, 75), (30, -10), (45, 5), (55, 40))
        sh.draw_bezier((55, 40), (65, 75), (80, 90), (98, 5))
    elif kind == "neg_cubic":
        sh.draw_bezier((12, 5), (30, 90), (45, 75), (55, 40))
        sh.draw_bezier((55, 40), (65, 5), (80, -10), (98, 75))
    elif kind == "parabola":
        sh.draw_bezier((15, 75), (35, -5), (75, -5), (95, 75))
    else:
        sh.draw_line((12, 40), (98, 40))
    sh.finish(color=(0, 0, 0), width=1.1, closePath=False)
    sh.commit()
    page.insert_text((100, 50), "x", fontsize=8, fontname="Times-Italic")
    page.insert_text((58, 9), "y", fontsize=8, fontname="Times-Italic")
    doc.save(path)


FIGURES_TEX = {
    "triangle": r"""\par\medskip\hspace*{25mm}\setlength{\unitlength}{1mm}\begin{picture}(60,34)
\put(0,0){\line(1,0){60}}\put(0,0){\line(1,1){30}}\put(60,0){\line(-1,1){30}}
\put(-3,-3){$A$}\put(61,-3){$B$}\put(29,31){$C$}\put(6,1.5){\small $45^\circ$}
\end{picture}\par\bigskip""",
    "graphs": r"""\par\medskip\hspace*{8mm}\begin{tabular}{cc}
\includegraphics[width=38mm]{g_cubic.pdf} & \includegraphics[width=38mm]{g_neg_cubic.pdf}\\
\textbf{A} & \textbf{B}\\[3mm]
\includegraphics[width=38mm]{g_parabola.pdf} & \includegraphics[width=38mm]{g_line.pdf}\\
\textbf{C} & \textbf{D}
\end{tabular}\par""",
    "table": r"""\par\medskip\hspace*{20mm}\begin{tabular}{|c|c|c|c|c|}\hline
$x$ & 0 & 1 & 2 & 3\\\hline $f(x)$ & 1 & 3 & 5 & 7\\\hline\end{tabular}\par\medskip""",
}


# --------------------------------------------------------------------------- tex
def to_tex(text: str, statements: bool = True) -> str:
    """.tmua.json text -> LaTeX source (paragraphs, display maths)."""
    paras = text.split("\n\n")
    out = []
    for p in paras:
        p = re.sub(r"\$\$(.+?)\$\$", r"\\[\1\\]", p, flags=re.S)
        m = re.match(r"^(I{1,3}) (.*)$", p) if statements else None
        if m:
            p = f"\\hspace*{{4mm}}\\makebox[8mm][l]{{{m.group(1)}}}{m.group(2)}"
        out.append(p)
    return "\n\n".join(out)


def build_tex(font: str, screenshot_q1: bool = False) -> str:
    font_pkg = {"times": "\\usepackage{mathptmx}\n",
                "unicode": "\\usepackage{unicode-math}\n\\setmathfont{latinmodern-math.otf}\n"}.get(font, "")
    rows = font == "unicode"  # this variant also lays the options out across the line
    body = []
    for n, q in enumerate(QUESTIONS, start=1):
        if n == 1 and screenshot_q1:  # the question pasted in as a picture, as in many Word-made papers
            body.append("\\item[\\textbf{1}] \\raisebox{-\\height}[0pt][0pt]{}\\includegraphics[width=120mm]{q1.png}"
                        "\n\\vspace{10mm}")
            continue
        paras = q["stem"].split("\n\n")
        parts = []
        for i, p in enumerate(paras):
            parts.append(to_tex(p))
            if q.get("figure") and q.get("figure_after_paragraph", len(paras) - 1) == i:
                parts.append(FIGURES_TEX[q["figure"]])
        stem = "\n\n".join(parts)
        opts = ""
        if q.get("figure") != "graphs":
            if q.get("page_break_before_options"):
                opts += "\\newpage\n"
            if rows:
                cells = [f"\\textbf{{{chr(65 + i)}}}\\hspace{{4mm}}{to_tex(o, statements=False)}"
                         for i, o in enumerate(q["options"])]
                opts += "\\par\\medskip\n\\noindent " + "".join(
                    c + ("\\par\\smallskip\\noindent " if (i + 1) % 3 == 0 else "\\hspace{12mm}")
                    for i, c in enumerate(cells)) + "\\par"
            else:
                opts += "\\par\\medskip\n" + "\n".join(
                    f"\\noindent\\hspace*{{0mm}}\\makebox[9mm][l]{{\\textbf{{{chr(65 + i)}}}}}"
                    f"{to_tex(o, statements=False)}\\par\\smallskip" for i, o in enumerate(q["options"]))
        body.append(f"\\item[\\textbf{{{n}}}] {stem}\n{opts}\n\\vspace{{10mm}}")
        if n in (2, 5):
            body.append("\\end{list}\\newpage\\begin{list}{}{\\setlength{\\leftmargin}{14mm}\\setlength{\\labelsep}{6mm}"
                        "\\setlength{\\labelwidth}{8mm}}")
        if n == 3:
            body.append("\\end{list}\\newpage\\vspace*{80mm}\\begin{center}\\textbf{BLANK PAGE}\\end{center}\\newpage"
                        "\\begin{list}{}{\\setlength{\\leftmargin}{14mm}\\setlength{\\labelsep}{6mm}"
                        "\\setlength{\\labelwidth}{8mm}}")
    items = "\n".join(body)
    return rf"""\documentclass[11pt,a4paper]{{article}}
\usepackage[margin=25mm]{{geometry}}
\usepackage{{amsmath,amssymb,graphicx}}
{font_pkg}\setlength{{\parindent}}{{0pt}}
\begin{{document}}
\thispagestyle{{empty}}
\vspace*{{30mm}}
\begin{{center}}
{{\LARGE Sample Admissions Test\par}}\medskip
{{\Large Paper 1\par}}\bigskip
Sample Set 3 (2026)\par\bigskip
Time allowed: 1 hour 15 minutes\par\medskip
There are 8 questions. Answer all of them.\par
Calculators are not permitted.
\end{{center}}
\newpage
\begin{{list}}{{}}{{\setlength{{\leftmargin}}{{14mm}}\setlength{{\labelsep}}{{6mm}}\setlength{{\labelwidth}}{{8mm}}}}
{items}
\end{{list}}
\vspace{{20mm}}\begin{{center}}END OF TEST\end{{center}}
\end{{document}}
"""


def expected_json() -> dict:
    return {
        "title": TITLE, **META,
        "questions": [{"number": n, "stem": q["stem"],
                       "options": [{"label": chr(65 + i), "content": o} for i, o in enumerate(q["options"])],
                       "has_image": "figure" in q}
                      for n, q in enumerate(QUESTIONS, start=1)],
    }


def screenshot_question_1(pdf: Path, out: Path) -> None:
    """A 150 dpi picture of question 1's body (without its number), like a pasted screenshot."""
    import sys

    import pymupdf
    sys.path.insert(0, str(HERE.parent))
    from tmua_converter.pdf import PdfDocument

    with PdfDocument(pdf) as d:
        a = d.find_question_anchors()[1]
        (page, box), = d.question_regions(1)
    clip = pymupdf.Rect(a.box.x1 + 8, box.y0, box.x1 + 4, box.y1)
    pymupdf.open(str(pdf))[page - 1].get_pixmap(dpi=150, clip=clip).save(str(out))


def build() -> None:
    pdflatex = os.environ.get("TMUA_PDFLATEX") or shutil.which("pdflatex")
    if not pdflatex:
        raise SystemExit("pdflatex not found (set TMUA_PDFLATEX)")
    DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for kind in ("cubic", "neg_cubic", "parabola", "line"):
            make_graph_pdf(tmp / f"g_{kind}.pdf", kind)
        xelatex = os.environ.get("TMUA_XELATEX") or str(Path(pdflatex).with_name("xelatex"))
        for font in ("cm", "times", "unicode", "mixed"):
            if font == "mixed":
                screenshot_question_1(DATA / "latex_cm.pdf", tmp / "q1.png")
            (tmp / "paper.tex").write_text(build_tex("cm" if font == "mixed" else font, font == "mixed"),
                                           encoding="utf-8")
            (tmp / "paper.pdf").unlink(missing_ok=True)
            engine = xelatex if font == "unicode" else pdflatex
            for _ in range(2):
                r = subprocess.run([engine, "-interaction=nonstopmode", "paper.tex"], cwd=tmp,
                                   capture_output=True, text=True)
            if not (tmp / "paper.pdf").exists():  # mathptmx warns about fonts it never uses; only a missing PDF is fatal
                raise SystemExit(r.stdout[-3000:])
            shutil.copy(tmp / "paper.pdf", DATA / f"latex_{font}.pdf")
    (DATA / "latex_expected.json").write_text(json.dumps(expected_json(), indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
    print("wrote latex_cm/times/unicode/mixed.pdf and latex_expected.json in", DATA)


if __name__ == "__main__":
    build()
