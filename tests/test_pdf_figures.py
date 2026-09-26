import pytest

from tmua_converter.figures import crop_figure
from tmua_converter.pdf import Box, PdfDocument


@pytest.fixture(scope="module")
def doc(sample_pdf):
    d = PdfDocument(sample_pdf)
    yield d
    d.close()


def test_anchors_ignore_maths_digits(doc):
    anchors = doc.find_question_anchors()
    assert {n: a.page for n, a in anchors.items()} == {1: 2, 2: 2, 3: 3, 4: 5}


def test_blank_page_detected(doc):
    assert [doc.is_blank_page(p) for p in range(1, 6)] == [False, False, False, True, False]


def test_question_regions(doc):
    r1 = doc.question_regions(1)
    r2 = doc.question_regions(2)
    assert [p for p, _ in r1] == [2] and [p for p, _ in r2] == [2]
    assert r1[0][1].y1 <= r2[0][1].y0 + 1  # q1 ends where q2 starts
    assert [p for p, _ in doc.question_regions(3)] == [3]  # does not spill onto the blank page


def test_model_coordinates_roundtrip(doc):
    b = Box(100, 200, 300, 400)
    back = doc.px_to_pt(2, doc.pt_to_px(2, b))
    assert all(abs(x - y) < 1e-6 for x, y in zip(b.to_list(6), back.to_list(6)))
    assert max(doc.model_image_size(2)) == 2576


TRIANGLE = Box(146.5, 395.2, 343.4, 520.7)


@pytest.mark.parametrize("rough", [
    Box(170, 420, 320, 500),   # too tight: labels outside
    Box(100, 370, 450, 520),   # loose: overlaps the question text above
    Box(60, 320, 540, 680),    # very loose: includes stem and options
])
def test_triangle_crop_is_tight_and_complete(doc, rough):
    crop = crop_figure(doc, 2, rough)
    b = crop.box_pt
    # same final crop regardless of the rough box
    assert all(abs(x - y) < 3 for x, y in zip(b.to_list(), TRIANGLE.to_list())), b
    # none of the question text lines are inside the crop
    for ln in doc.content_lines(2):
        if len(ln.text) > 12:
            assert b.intersect(ln.box).is_empty(), ln.text
    # the vertex labels are
    labels = [ln for ln in doc.content_lines(2) if ln.text in ("A", "B", "C") and ln.box.y0 > 380 and ln.box.y1 < 530]
    assert len(labels) == 3 and all(b.contains(ln.box, tol=0.5) for ln in labels)
    assert crop.metrics["edges_cutting_ink"] == []
    assert crop.metrics["whitespace_fraction"] < 0.3
    assert crop.png.startswith(b"\x89PNG")


def test_graph_panel_crop_includes_all_graph_letters(doc):
    crop = crop_figure(doc, 3, Box(125, 90, 410, 330))
    letters = [ln for ln in doc.content_lines(3) if ln.text in ("A", "B", "C", "D") and ln.box.x0 > 150]
    assert len(letters) == 4
    assert all(crop.box_pt.contains(ln.box, tol=0.5) for ln in letters)
    stem = next(ln for ln in doc.content_lines(3) if ln.text.startswith("Which one"))
    assert crop.box_pt.intersect(stem.box).is_empty()
    options = [ln for ln in doc.content_lines(3) if ln.text.startswith("Graph ")]
    assert all(crop.box_pt.intersect(o.box).is_empty() for o in options)


def test_exact_crop_only_trims(doc):
    crop = crop_figure(doc, 2, Box(150, 400, 340, 515), refine=False)
    assert crop.box_pt.x0 >= 145 and crop.box_pt.x1 <= 345


def test_question_word_anchors(tmp_path):
    import pymupdf

    d = pymupdf.open()
    for i in (1, 2):
        pg = d.new_page()
        pg.insert_text((50, 80), f"Question {i}", fontsize=12)
        pg.insert_text((70, 110), "Some text 3 with 2 digits", fontsize=11)
    path = tmp_path / "q.pdf"
    d.save(path)
    with PdfDocument(path) as doc:
        assert {n: a.page for n, a in doc.find_question_anchors().items()} == {1: 1, 2: 2}
