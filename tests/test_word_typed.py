"""Papers typed in a word processor (Times New Roman, no equation editor): underlines,
units, typed powers (n²), ordinals and punctuation next to maths."""

from pathlib import Path

import pymupdf
import pytest

from tmua_converter.pipeline import ConvertOptions, convert_pdf

FONT_DIR = Path("/usr/share/fonts/truetype/liberation")
pytestmark = pytest.mark.skipif(not (FONT_DIR / "LiberationSerif-Regular.ttf").exists(),
                                reason="needs Liberation Serif (metric clone of Times New Roman)")


class Page:
    def __init__(self):
        self.doc = pymupdf.open()
        self.page = self.doc.new_page()
        self.fonts = {}

    def t(self, x, y, s, style="Regular", size=11.0):
        path = FONT_DIR / f"LiberationSerif-{style}.ttf"
        font = self.fonts.setdefault(style, pymupdf.Font(fontfile=str(path)))
        self.page.insert_text((x, y), s, fontname="F" + style, fontfile=str(path), fontsize=size)
        return x + font.text_length(s, fontsize=size)


def convert(tmp_path, page):
    page.doc.save(tmp_path / "p.pdf")
    return convert_pdf(tmp_path / "p.pdf", ConvertOptions(render_check=False), tmp_path).paper.questions


def test_word_typed_question(tmp_path):
    p = Page()
    y = 60
    p.t(50, y, "1", "Bold")
    x = p.t(70, y, "Which one of the following is ")
    x = p.t(x, y, "NOT", "Bold", 12)
    x = p.t(x, y + 0.6, " true for every integer ")  # baseline jitter
    x = p.t(x, y, "n", "Italic")
    x = p.t(x, y, "? The student’s answer is ")
    x0 = x
    x = p.t(x, y, "not")
    p.page.draw_line((x0, y + 1.5), (x, y + 1.5), width=0.5)  # underlined
    p.t(x, y, " correct.")
    y += 18
    x = p.t(70, y, "The area is 12 cm")
    x = p.t(x, y - 4, "2", size=7.3)
    x = p.t(x, y, ". [Note: log")
    x = p.t(x, y + 3, "10", size=7.3)
    x = p.t(x, y, " means base 10.] e.g. the 2")
    x = p.t(x, y - 4, "nd", size=7.3)
    p.t(x, y, " term.")
    opts = ["n² + n is even", "n³ − n is divisible by 3", "2ⁿ > n", "n(n + 1)(n + 2) is odd",
            "none of the above"]
    for i, o in enumerate(opts):
        p.t(70, y + 24 + 18 * i, "ABCDE"[i], "Bold")
        p.t(95, y + 24 + 18 * i, o)
    q = convert(tmp_path, p)[0]
    assert q.stem == ("Which one of the following is NOT true for every integer $n$? The student’s answer is not "
                      "correct. The area is 12 cm$^2$. [Note: $\\log_{10}$ means base 10.] e.g. the 2nd term.")
    assert [o.content for o in q.options] == ["$n^2 + n$ is even", "$n^3 - n$ is divisible by 3", "$2^n > n$",
                                              "$n(n + 1)(n + 2)$ is odd", "none of the above"]
    assert not q.needsReview
