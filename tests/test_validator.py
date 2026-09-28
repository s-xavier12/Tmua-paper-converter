import base64
import copy
import io

import pytest
from PIL import Image as PILImage

from tmua_converter.pdf import PdfDocument
from tmua_converter.validator import ValidationReport, check_string, validate_paper


def codes(s: str) -> list[tuple[str, str]]:
    r = ValidationReport()
    check_string(r, s, 1, "stem")
    return [(i.level, i.code) for i in r.issues]


def error_codes(s: str) -> set[str]:
    return {c for lvl, c in codes(s) if lvl == "error"}


@pytest.mark.parametrize("s", [
    "Find $x$ such that\n\n$$\\frac{x^2 - 4}{x + 1} \\le 0$$",
    "$x \\ne 2$ and $x \\neq 3$ and $\\nu + \\nabla f$ and $\\neg p$",
    "$\\log_{2} 11$ and $\\left| x \\right|$ and $\\lfloor x \\rfloor$",
    "$\\text{sin is fine here}$ and $\\operatorname{sgn}(x)$",
    "$a$$b$",
    "$\\{1, 2, 3\\}$",
    "Statement I\n\nStatement II",
])
def test_clean_strings_pass(s):
    assert error_codes(s) == set()


@pytest.mark.parametrize("s, code", [
    ("Find $x$.\\n\\nThen stop.", "literal-backslash-n"),
    ("Line one\\nThe next", "literal-backslash-n"),
    ("$\\\\frac{1}{2}$", "double-backslash"),
    ("$2 \times 3$", "control-char"),          # \t of \times parsed as TAB
    ("$\frac{1}{2}$", "control-char"),          # \f of \frac parsed as form feed
    ("$\beta$", "control-char"),                # \b of \beta parsed as backspace
    ("$x \neq 2$", "eaten-backslash-n"),        # \n of \neq parsed as newline
    ("Find $x such that", "dollar-balance"),
    ("$\\frac{1}{2$", "dollar-balance"),        # unclosed brace swallows the closing $
    ("Costs \\$5 and $x$", "dollar-balance"),   # \$ is not an escape for auto-render
    ("$$ a $ b $$", "dollar-balance"),
    ("$frac{1}{2}$", "bare-command"),
    ("$\\sqrt 2 + sqrt{3}$", "bare-command"),
    ("$x^2 + infty$", "bare-command"),
    ("Compute \\frac{1}{2}", "command-outside-math"),
    ("a​b", "invisible-char"),
    ("a�b", "replacement-char"),
    ("ab", "private-char"),
    ("$\\left( x$", "left-right"),
    ("$\\begin{cases} x \\end{aligned}$", "environment"),
    ("", "empty"),
])
def test_bad_strings_are_errors(s, code):
    assert code in error_codes(s)


def test_warnings_not_errors():
    assert ("warning", "bare-function") in codes("$sin x$")
    assert ("warning", "latex-linebreak") in codes("$$a \\\\ b$$")
    assert ("warning", "unknown-command") in codes("$\\frca{1}{2}$")
    assert error_codes("$sin x$") == set()


def _png(w=200, h=120) -> str:
    img = PILImage.new("RGB", (w, h), "white")
    for x in range(10, w - 10):
        for y in (10, h - 10):
            img.putpixel((x, y), (0, 0, 0))
    for y in range(10, h - 10):
        for x in (10, w - 10):
            img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def good_paper() -> dict:
    return {
        "formatVersion": 1, "id": "sample-paper-1", "title": "Sample", "paper": "Paper 1", "year": "2026",
        "durationMinutes": 40,
        "questions": [
            {"number": 1, "stem": "Find $x$.\n\n$$x^2 = 4$$",
             "options": [{"label": "A", "content": "$2$"}, {"label": "B", "content": "$-2$"}],
             "images": [], "sourcePage": 2, "needsReview": False},
            {"number": 2, "stem": "The diagram shows a square.",
             "options": [{"label": "A", "content": "$1$"}, {"label": "B", "content": "$2$"}],
             "images": [{"src": _png(), "alt": "A square drawn with thin lines"}], "sourcePage": 2,
             "needsReview": False},
        ],
    }


def test_good_paper_passes():
    r = validate_paper(good_paper(), expected_questions=2, expected_duration=40)
    assert r.ok, r.format()


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.__setitem__("formatVersion", "1"), "format-version"),
    (lambda d: d["questions"][0].__setitem__("correctAnswer", "A"), "answer-leak"),
    (lambda d: d["questions"][1].__setitem__("number", 3), "numbering"),
    (lambda d: d["questions"][0]["options"][1].__setitem__("label", "C"), "option-label"),
    (lambda d: d["questions"][0].__setitem__("options", []), "options"),
    (lambda d: d.__setitem__("durationMinutes", 75), "duration-mismatch"),
    (lambda d: d["questions"].pop(), "question-count"),
    (lambda d: d.__setitem__("solutions", []), "extra-keys"),
    (lambda d: d["questions"][1]["images"][0].__setitem__("src", "data:image/png;base64,AAAA"), "image-decode"),
    (lambda d: d["questions"][1]["images"][0].__setitem__("alt", ""), "image-alt"),
    (lambda d: d["questions"][0].__setitem__("sourcePage", 0), "source-page"),
    (lambda d: d["questions"][0].__setitem__("options", [{"label": "A", "content": "Graph A"},
                                                          {"label": "B", "content": "Graph B"}]),
     "graph-options-missing"),
])
def test_paper_errors(mutate, code):
    d = copy.deepcopy(good_paper())
    mutate(d)
    r = validate_paper(d, expected_questions=2, expected_duration=40)
    assert code in {i.code for i in r.errors}, r.format()


def test_source_page_checked_against_pdf(sample_pdf):
    d = good_paper()
    # the sample PDF has question 1 on page 2 and question 2 on page 2
    with PdfDocument(sample_pdf) as doc:
        assert validate_paper(d, pdf=doc).ok
        d["questions"][1]["sourcePage"] = 3
        r = validate_paper(d, pdf=doc)
    assert any(i.code == "source-page" and i.question == 2 for i in r.errors)


def test_whitespace_heavy_image_warns():
    d = good_paper()
    img = PILImage.new("RGB", (600, 600), "white")
    for x in range(10, 60):
        img.putpixel((x, 20), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    d["questions"][1]["images"][0]["src"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    r = validate_paper(d)
    assert "image-whitespace" in {i.code for i in r.warnings}


def test_command_katex_cannot_draw_is_an_error():
    from tmua_converter.validator import ValidationReport, check_string

    r = ValidationReport()
    check_string(r, "Let $\\lcm(a, b)$ and $\\operatorname{hcf}(a, b)$ be given.", 1, "stem")
    assert [i.code for i in r.errors] == ["katex-unsupported"]


def test_katex_through_node_catches_what_it_cannot_draw():
    import pytest

    from tmua_converter.katex_node import check_paper, node_available

    if not node_available():
        pytest.skip("needs Node.js")
    data = {"questions": [{"number": 1, "stem": "Fine $\\frac{1}{2}$, bad $x^$ and $\\lcm(a)$",
                           "options": [{"label": "A", "content": "$$\\int_0^1 x\\,dx$$"}]}]}
    errors = check_paper(data)
    assert set(errors) == {(1, "stem")}
    assert "x^" in errors[(1, "stem")] and "lcm" in errors[(1, "stem")]
