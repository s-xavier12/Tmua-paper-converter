"""Build a Word-style test paper: a .docx with native Word equations (OMML),
exported to PDF by LibreOffice - the way many mock papers are made.

Uses the same questions and expected answers as make_latex_papers.py, but a
different layout: question numbers typed as "1." in the same line as the
text, options as "A<tab>..." paragraphs, figures as pasted pictures.

    python tests/make_word_paper.py     # needs: pip install python-docx latex2mathml; LibreOffice (soffice)
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from make_latex_papers import QUESTIONS, make_graph_pdf  # noqa: E402

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MML = "{http://www.w3.org/1998/Math/MathML}"
NARY = {"∑", "∏", "∫"}


def _m(tag: str) -> str:
    return f"<m:{tag}>"


def _run(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<m:r><m:t xml:space="preserve">{text}</m:t></m:r>'


def _omml(el) -> str:
    tag = el.tag.replace(MML, "")
    kids = list(el)
    if tag in ("math", "mrow", "mstyle", "semantics"):
        return _seq(kids)
    if tag in ("mi", "mn", "mo", "mtext"):
        return _run(el.text or "")
    if tag == "mspace":
        return ""
    if tag == "mfrac":
        return f"<m:f><m:num>{_omml(kids[0])}</m:num><m:den>{_omml(kids[1])}</m:den></m:f>"
    if tag == "msqrt":
        return f"<m:rad><m:radPr><m:degHide m:val=\"1\"/></m:radPr><m:deg/><m:e>{_seq(kids)}</m:e></m:rad>"
    if tag in ("msub", "msup", "msubsup", "munderover", "munder", "mover"):
        base = kids[0]
        if base.tag == MML + "mo" and (base.text or "") in NARY:
            sub = _omml(kids[1]) if len(kids) > 1 else ""
            sup = _omml(kids[2]) if len(kids) > 2 else ""
            return (f"<m:nary><m:naryPr><m:chr m:val=\"{base.text}\"/></m:naryPr><m:sub>{sub}</m:sub>"
                    f"<m:sup>{sup}</m:sup><m:e></m:e></m:nary>")
        if tag in ("msub", "munder"):
            return f"<m:sSub><m:e>{_omml(base)}</m:e><m:sub>{_omml(kids[1])}</m:sub></m:sSub>"
        if tag in ("msup", "mover"):
            return f"<m:sSup><m:e>{_omml(base)}</m:e><m:sup>{_omml(kids[1])}</m:sup></m:sSup>"
        return (f"<m:sSubSup><m:e>{_omml(base)}</m:e><m:sub>{_omml(kids[1])}</m:sub>"
                f"<m:sup>{_omml(kids[2])}</m:sup></m:sSubSup>")
    return _seq(kids)


def _nary(el):
    tag = el.tag.replace(MML, "")
    kids = list(el)
    if tag in ("msubsup", "munderover", "msub", "munder") and kids and kids[0].tag == MML + "mo" \
            and (kids[0].text or "").strip() in NARY:
        return (_omml(kids[1]) if len(kids) > 1 else "", _omml(kids[2]) if len(kids) > 2 else "")
    return None


def _seq(kids) -> str:
    """Children in order; a big operator absorbs the next item as its body; stretchy ( ) become a delimiter."""
    out, i = [], 0
    while i < len(kids):
        k = kids[i]
        nary = _nary(k)
        if nary is not None:  # a big operator takes the rest of the row as its body, as Word does
            sub, sup = nary
            chr_ = (k[0].text or "").strip()
            out.append(f"<m:nary><m:naryPr><m:chr m:val=\"{chr_}\"/></m:naryPr><m:sub>{sub}</m:sub>"
                       f"<m:sup>{sup}</m:sup><m:e>{_seq(kids[i + 1:])}</m:e></m:nary>")
            break
        if k.tag == MML + "mo" and k.get("stretchy") == "true" and (k.text or "") == "(":
            depth, j = 1, i + 1
            while j < len(kids):
                if kids[j].tag == MML + "mo" and kids[j].get("stretchy") == "true":
                    depth += 1 if kids[j].text == "(" else -1 if kids[j].text == ")" else 0
                    if depth == 0:
                        break
                j += 1
            out.append(f"<m:d><m:e>{_seq(kids[i + 1:j])}</m:e></m:d>")
            i = j + 1
            continue
        out.append(_omml(k))
        i += 1
    return "".join(out)


def latex_to_omml(tex: str) -> str:
    from latex2mathml.converter import convert

    tex = tex.replace("\\left(", "(").replace("\\right)", ")")
    root = ET.fromstring(convert(tex))
    return f'<m:oMath xmlns:m="{M}">{_omml(root)}</m:oMath>'


def add_rich(paragraph, text: str) -> None:
    """Text with $...$ inline maths; $$...$$ becomes a centred equation paragraph handled by the caller."""
    from docx.oxml import parse_xml

    for i, part in enumerate(re.split(r"\$(.+?)\$", text)):
        if i % 2 == 0:
            if part:
                paragraph.add_run(part)
        else:
            paragraph._p.append(parse_xml(latex_to_omml(part)))


def build(out: Path) -> Path:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt

    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(11)
    tmp = Path(tempfile.mkdtemp())
    for kind in ("cubic", "neg_cubic", "parabola", "line"):
        make_graph_pdf(tmp / f"g_{kind}.pdf", kind)

    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("Sample Admissions Test")
    r.font.size = Pt(20)
    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("Paper 1")
    r.font.size = Pt(16)
    for line in ("Sample Set 3 (2026)", "Time allowed: 1 hour 15 minutes", "There are 8 questions. Answer all of them."):
        q = d.add_paragraph(line)
        q.alignment = WD_ALIGN_PARAGRAPH.CENTER
    d.add_page_break()

    for n, qd in enumerate(QUESTIONS, start=1):
        paras = qd["stem"].split("\n\n")
        for i, para in enumerate(paras):
            if para.startswith("$$"):
                eq = d.add_paragraph()
                eq.paragraph_format.left_indent = Mm(10)
                eq.alignment = WD_ALIGN_PARAGRAPH.CENTER
                add_rich(eq, "$" + para.strip("$") + "$")
            else:
                par = d.add_paragraph()
                par.paragraph_format.left_indent = Mm(10)
                par.paragraph_format.first_line_indent = Mm(-10) if i == 0 else Mm(0)
                par.paragraph_format.tab_stops.add_tab_stop(Mm(10))
                par.paragraph_format.tab_stops.add_tab_stop(Mm(20))
                if i == 0:
                    num = par.add_run(f"{n}.\t")
                    num.bold = True
                m = re.match(r"^(I{1,3}) (.*)$", para)
                add_rich(par, f"{m.group(1)}\t{m.group(2)}" if m else para)
            if qd.get("figure") and qd.get("figure_after_paragraph", len(paras) - 1) == i:
                _figure(d, qd["figure"], tmp)
        if qd.get("figure") != "graphs":
            if qd.get("page_break_before_options"):
                d.add_page_break()
            for k, o in enumerate(qd["options"]):
                par = d.add_paragraph()
                par.paragraph_format.left_indent = Mm(10)
                par.paragraph_format.tab_stops.add_tab_stop(Mm(20))
                lab = par.add_run(f"{chr(65 + k)}\t")
                lab.bold = True
                add_rich(par, o)
        d.add_paragraph()
        if n in (2, 3, 5):
            d.add_page_break()
    d.add_paragraph("END OF TEST").alignment = WD_ALIGN_PARAGRAPH.CENTER
    docx_path = tmp / "paper.docx"
    d.save(docx_path)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(docx_path)],
                   check=True, capture_output=True, timeout=180)
    shutil.copy(tmp / "paper.pdf", out)
    return out


def _figure(d, kind: str, tmp: Path) -> None:
    import pymupdf
    from docx.shared import Mm

    if kind == "triangle":
        doc = pymupdf.open()
        pg = doc.new_page(width=200, height=120)
        sh = pg.new_shape()
        sh.draw_polyline([(15, 105), (185, 105), (100, 20), (15, 105)])
        sh.finish(color=(0, 0, 0), width=1)
        sh.commit()
        for x, y, t in ((5, 116, "A"), (188, 116, "B"), (97, 15, "C"), (32, 100, "45°")):
            pg.insert_text((x, y), t, fontsize=10, fontname="Times-Italic" if len(t) == 1 else "Times-Roman")
        pix = pg.get_pixmap(dpi=200)
        path = tmp / "tri.png"
        pix.save(path)
        d.add_paragraph().add_run().add_picture(str(path), width=Mm(60))
    elif kind == "graphs":
        doc = pymupdf.open()
        pg = doc.new_page(width=260, height=210)
        for i, g in enumerate(("cubic", "neg_cubic", "parabola", "line")):
            x, y = (i % 2) * 130, (i // 2) * 105
            pg.show_pdf_page(pymupdf.Rect(x + 5, y, x + 115, y + 80), pymupdf.open(tmp / f"g_{g}.pdf"), 0)
            pg.insert_text((x + 55, y + 96), "ABCD"[i], fontsize=11, fontname="Times-Bold")
        path = tmp / "graphs.png"
        pg.get_pixmap(dpi=200).save(path)
        d.add_paragraph().add_run().add_picture(str(path), width=Mm(90))
    elif kind == "table":
        t = d.add_table(rows=2, cols=5)
        t.style = "Table Grid"
        for c, v in enumerate(["x", "0", "1", "2", "3"]):
            t.cell(0, c).text = v
        for c, v in enumerate(["f(x)", "1", "3", "5", "7"]):
            t.cell(1, c).text = v


if __name__ == "__main__":
    print(build(HERE / "data" / "word_paper.pdf"))
