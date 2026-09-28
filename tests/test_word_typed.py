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


def test_hcf_lcm_are_katex_safe(tmp_path):
    p = Page()
    y = 60
    p.t(50, y, "1", "Bold")
    x = p.t(70, y, "Let hcf(")
    x = p.t(x, y, "a", "Italic")
    x = p.t(x, y, ", ")
    x = p.t(x, y, "b", "Italic")
    x = p.t(x, y, ") and lcm(")
    x = p.t(x, y, "a", "Italic")
    x = p.t(x, y, ", ")
    x = p.t(x, y, "b", "Italic")
    p.t(x, y, ") be as usual.")
    for i in range(4):
        p.t(70, y + 30 + 18 * i, "ABCD"[i], "Bold")
        p.t(95, y + 30 + 18 * i, str(i + 2))
    q = convert(tmp_path, p)[0]
    assert q.stem == "Let $\\operatorname{hcf}(a, b)$ and $\\operatorname{lcm}(a, b)$ be as usual."
    assert not q.needsReview


def _numbered_pages(p_number_x, twice=False):
    """Three questions, one per page; each page number equals its question number."""
    doc = Page()
    doc.page.insert_text((50, 80), "Mock Paper", fontsize=20)
    stems = ["For positive integers a and b, find the pairs.", "Evaluate the sum of the series given here.",
             "Which value of x makes the product largest?"]
    for n in range(1, 4):
        doc.page = doc.doc.new_page()
        if p_number_x is not None:
            doc.t(p_number_x, 40, str(n))
        doc.t(50, 80, str(n), "Bold", 12)
        if twice:
            doc.t(80, 80, str(n), "Bold", 14)  # the number printed again, e.g. in a box
        doc.t(80, 100 if twice else 80, stems[n - 1])
        for i in range(4):
            doc.t(80, 130 + 20 * i, "ABCD"[i], "Bold")
            doc.t(105, 130 + 20 * i, str(n * 10 + i))
    return doc, stems


@pytest.mark.parametrize("p_number_x,twice", [(50, False), (295, False), (None, True)])
def test_question_number_is_not_in_the_text(tmp_path, p_number_x, twice):
    doc, stems = _numbered_pages(p_number_x, twice)
    qs = convert(tmp_path, doc)
    assert [q.stem for q in qs] == stems
    assert [[o.content for o in q.options] for q in qs] == [[f"${n * 10 + i}$" for i in range(4)] for n in (1, 2, 3)]
    assert not any(q.needsReview for q in qs)


def test_broken_unicode_is_repaired_from_the_font(tmp_path):
    """A PDF whose character table maps digits to nothing: readers then report the glyph number,
    which lands in scripts such as Oriya.  The real digits come back from the embedded font."""
    import re

    p = Page()
    p.t(50, 60, "1", "Bold")
    p.t(70, 60, "Work out 12 + 34 and 56 + 78.")
    for i in range(4):
        p.t(70, 90 + 18 * i, "ABCD"[i], "Bold")
        p.t(95, 90 + 18 * i, str(90 + i))
    p.doc.save(tmp_path / "good.pdf")
    doc = pymupdf.open(tmp_path / "good.pdf")
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "ToUnicode")[0] != "xref":
            continue
        cmap_xref = int(doc.xref_get_key(xref, "ToUnicode")[1].split()[0])
        cmap = doc.xref_stream(cmap_xref).decode("latin1")
        # point the digits at Oriya letters, as a broken PDF effectively does
        def split(m):
            a, b, u = (int(g, 16) for g in m.groups())
            if not u <= 0x30 and 0x39 <= u + b - a:
                return m.group(0)
            d0 = a + 0x30 - u
            return (f"<{a:04x}> <{d0 - 1:04x}> <{u:04x}>\n<{d0:04x}> <{d0 + 9:04x}> <0b30>\n"
                    f"<{d0 + 10:04x}> <{b:04x}> <003a>")
        cmap = re.sub(r"<([0-9a-fA-F]{4})> <([0-9a-fA-F]{4})> <([0-9a-fA-F]{4})>", split, cmap)
        cmap = re.sub(r"(\d+) beginbfrange", lambda m: f"{int(m.group(1)) + 2} beginbfrange", cmap, count=1)
        doc.update_stream(cmap_xref, cmap.encode("latin1"))
    for xref in range(1, doc.xref_length()):  # PDF writers often rename fonts, e.g. "CIDFont+F4"
        for key in ("BaseFont", "FontName"):
            if doc.xref_get_key(xref, key)[0] == "name":
                doc.xref_set_key(xref, key, "/CIDFont+F4")
    doc.save(tmp_path / "p.pdf")
    assert "12" not in pymupdf.open(tmp_path / "p.pdf")[0].get_text()  # the text layer really is broken
    q = convert_pdf(tmp_path / "p.pdf", ConvertOptions(render_check=False), tmp_path).paper.questions[0]
    assert q.stem == "Work out $12 + 34$ and $56 + 78$."
    assert [o.content for o in q.options] == ["$90$", "$91$", "$92$", "$93$"]
