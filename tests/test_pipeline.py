"""End-to-end conversion of LaTeX-typeset papers against their known-correct transcription."""

import json
import re
from pathlib import Path

import pymupdf
import pytest

from tmua_converter.ocr import find_tessdata
from tmua_converter.pipeline import ConvertOptions, convert_pdf, parse_duration, parse_question_count
from tmua_converter.schema import load_paper_dict
from tmua_converter.validator import validate_paper

DATA = Path(__file__).parent / "data"
EXPECTED = json.loads((DATA / "latex_expected.json").read_text(encoding="utf-8"))
needs_ocr = pytest.mark.skipif(find_tessdata() is None, reason="Tesseract OCR not installed")


def norm(s: str) -> str:
    """Compare maths ignoring spacing and optional braces around one-character scripts."""
    s = s.replace("\\leq", "\\le").replace("\\geq", "\\ge").replace("\\neq", "\\ne").replace("\\,", " ")
    s = re.sub(r"\^\{(.)\}", r"^\1", s)
    s = re.sub(r"_\{(.)\}", r"_\1", s)
    return re.sub(r"\s+", "", s)


def convert(pdf, tmp_path, **kw):
    return convert_pdf(pdf, ConvertOptions(render_check=False, **kw), out_dir=tmp_path)


@pytest.mark.parametrize("variant", ["cm", "times", "unicode"])
def test_every_question_exact(variant, tmp_path):
    """Three typesetting styles: LaTeX fonts, Times, Word-style Unicode maths (options across the line)."""
    res = convert(DATA / f"latex_{variant}.pdf", tmp_path)
    data = load_paper_dict(res.output_path)
    assert (data["title"], data["paper"], data["year"], data["durationMinutes"]) == \
        (EXPECTED["title"], "Paper 1", "2026", 75)
    assert len(data["questions"]) == len(EXPECTED["questions"])
    for q, e in zip(data["questions"], EXPECTED["questions"]):
        assert norm(q["stem"]) == norm(e["stem"]), (q["number"], q["stem"])
        assert [o["label"] for o in q["options"]] == [o["label"] for o in e["options"]]
        assert [norm(o["content"]) for o in q["options"]] == [norm(o["content"]) for o in e["options"]], q["number"]
        assert bool(q["images"]) == e["has_image"], q["number"]
        assert q["needsReview"] is False, (q["number"], res.questions[q["number"]].review_reasons)
    assert res.report.ok, res.report.format()


def test_written_file_escaping_and_pages(tmp_path):
    res = convert(DATA / "latex_cm.pdf", tmp_path)
    raw = res.output_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    stem = data["questions"][0]["stem"]
    assert "\n\n" in stem and "\\n" not in stem
    assert "\\frac" in stem and "\\\\frac" not in stem and "\\\\frac" in raw
    assert [q["sourcePage"] for q in data["questions"]] == [2, 2, 3, 5, 5, 6, 6, 7]
    assert "correctAnswer" not in raw
    # question 7's options are printed on the next page
    assert res.questions[7].regions[-1][0] == 7
    with pymupdf.open(DATA / "latex_cm.pdf"):
        pass


def test_figures_are_tight_crops(tmp_path):
    res = convert(DATA / "latex_cm.pdf", tmp_path)
    for n in (2, 3, 8):
        f = res.questions[n].figures[0]
        assert f.crop.metrics["edges_cutting_ink"] == []
        assert f.crop.metrics["whitespace_fraction"] < 0.4
        assert f.crop.metrics["page_height_fraction"] < 0.3
    assert res.questions[3].figures[0].labels == ["A", "B", "C", "D"]


def test_checks_catch_a_wrong_character(tmp_path):
    """Pass 2 compares the output with the characters printed in the PDF."""
    from tmua_converter import pipeline

    res = convert(DATA / "latex_cm.pdf", tmp_path)
    q = res.questions[1]
    q.stem = q.stem.replace("x + 1", "x + 7")
    q.review_reasons.clear()
    conv = pipeline.Converter(ConvertOptions(render_check=False))
    conv.doc = pipeline.PdfDocument(DATA / "latex_cm.pdf")
    conv._glyph_cache, conv._ignored, conv._ocr_rules = {}, set(), {}
    conv._cross_check(q)
    assert any("missing 1" in r and "extra 7" in r for r in q.review_reasons)


def test_metadata_parsing():
    assert parse_duration("Time allowed: 1 hour 15 minutes")[0] == 75
    assert parse_duration("You have 75 minutes")[0] == 75
    assert parse_duration("Time: 2 hours")[0] == 120
    assert parse_question_count("There are twenty questions")[0] == 20
    assert parse_question_count("This paper has 16 multiple-choice questions")[0] == 16


def test_overrides(tmp_path):
    res = convert(DATA / "latex_cm.pdf", tmp_path, title="Hercules Set 2", paper="Paper 2", year="Set 2",
                  duration_minutes=90, filename="custom.tmua.json")
    data = load_paper_dict(res.output_path)
    assert res.output_path.name == "custom.tmua.json"
    assert (data["title"], data["paper"], data["year"], data["durationMinutes"]) == \
        ("Hercules Set 2", "Paper 2", "Set 2", 90)


# ------------------------------------------------------------------------ pictures (OCR)
@needs_ocr
def test_pasted_screenshot_question(tmp_path):
    """Question 1 is a pasted picture: read with OCR and flagged; the rest stays exact."""
    res = convert(DATA / "latex_mixed.pdf", tmp_path)
    data = load_paper_dict(res.output_path)
    q1 = data["questions"][0]
    assert q1["stem"].startswith("Find the complete set of values of")
    assert [o["label"] for o in q1["options"]] == list("ABCDE")
    assert q1["needsReview"] is True
    for q, e in zip(data["questions"][1:], EXPECTED["questions"][1:]):
        assert norm(q["stem"]) == norm(e["stem"])
        assert q["needsReview"] is False


@needs_ocr
def test_scanned_paper(tmp_path):
    src = pymupdf.open(DATA / "latex_cm.pdf")
    scan = pymupdf.open()
    for pg in src:
        page = scan.new_page(width=pg.rect.width, height=pg.rect.height)
        page.insert_image(page.rect, pixmap=pg.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY))
    scan.save(tmp_path / "scan.pdf")
    res = convert(tmp_path / "scan.pdf", tmp_path)
    data = load_paper_dict(res.output_path)
    assert data["durationMinutes"] == 75
    assert [q["sourcePage"] for q in data["questions"]] == [2, 2, 3, 5, 5, 6, 6, 7]
    assert all(q["needsReview"] for q in data["questions"])  # OCR'd maths is always checked by a person
    assert [q["number"] for q in data["questions"] if q["images"]] == [2, 3, 8]
    assert [o["content"] for o in data["questions"][2]["options"]] == ["Graph A", "Graph B", "Graph C", "Graph D"]
    assert data["questions"][4]["stem"].startswith("Consider the following statements")
    # anything OCR could not read at all is left empty (never invented) and the question is flagged
    for issue in validate_paper(data).errors:
        assert issue.code == "empty" and data["questions"][issue.question - 1]["needsReview"]


@needs_ocr
def test_photo_input(tmp_path):
    png = tmp_path / "photo.png"
    pymupdf.open(DATA / "latex_cm.pdf")[1].get_pixmap(dpi=200).save(png)
    res = convert(png, tmp_path)
    data = load_paper_dict(res.output_path)
    assert [q["number"] for q in data["questions"]] == [1, 2]
    assert [len(q["options"]) for q in data["questions"]] == [5, 4]
    assert data["questions"][1]["images"]
    assert res.output_path.name == "photo_Paper_1.tmua.json"


def test_picture_without_ocr_gives_clear_error(tmp_path, monkeypatch):
    import tmua_converter.ocr as ocr

    monkeypatch.setattr(ocr, "find_tessdata", lambda: None)
    png = tmp_path / "photo.png"
    pymupdf.open(DATA / "latex_cm.pdf")[1].get_pixmap(dpi=100).save(png)
    with pytest.raises(Exception, match="Tesseract"):
        convert(png, tmp_path)
