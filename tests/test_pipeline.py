"""End-to-end conversion of the sample paper with a scripted Claude."""

import json

import pytest
from conftest import CHROMIUM
from fake_claude import FakeTransport, Script
from make_sample_paper import EXPECTED

from tmua_converter.combine import combine_papers
from tmua_converter.llm import ClaudeRunner
from tmua_converter.naming import paper_filename, paper_id
from tmua_converter.pipeline import ConvertOptions, convert_pdf
from tmua_converter.schema import Paper, load_paper_dict
from tmua_converter.validator import validate_paper


def run(sample_pdf, tmp_path, script=None, **opts):
    transport = FakeTransport(sample_pdf, script)
    runner = ClaudeRunner(transport, model="claude-opus-5")
    options = ConvertOptions(concurrency=2, render_check=CHROMIUM, **opts)
    return convert_pdf(sample_pdf, runner, options, out_dir=tmp_path), transport


@pytest.fixture(scope="module")
def converted(sample_pdf, tmp_path_factory):
    return run(sample_pdf, tmp_path_factory.mktemp("out"))


def test_output_matches_source(converted):
    res, _ = converted
    data = load_paper_dict(res.output_path)
    assert res.output_path.name == "Sample_Mathematics_Admissions_Paper_Paper_1.tmua.json"
    assert list(data) == ["formatVersion", "id", "title", "paper", "year", "durationMinutes", "questions"]
    assert data["formatVersion"] == 1 and data["durationMinutes"] == 40 and data["paper"] == "Paper 1"
    assert len(data["questions"]) == len(EXPECTED["questions"])
    for q, truth in zip(data["questions"], EXPECTED["questions"]):
        assert q["number"] == truth["number"]
        assert q["stem"] == truth["stem"]
        assert [o["content"] for o in q["options"]] == truth["options"]
        assert [o["label"] for o in q["options"]] == [chr(65 + i) for i in range(len(truth["options"]))]
        assert q["sourcePage"] == truth["page"]
        assert q["needsReview"] is False
        assert bool(q["images"]) == ("figure" in truth)
        assert "correctAnswer" not in q


def test_written_file_escaping(converted):
    res, _ = converted
    raw = res.output_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    stem = data["questions"][0]["stem"]
    assert "\n\n" in stem and "\\n" not in stem          # real newlines after parsing
    assert "\\frac" in stem and "\\\\frac" not in stem    # exactly one backslash after parsing
    assert '\\\\frac' in raw                               # ...which is escaped once in the raw file


def test_pass2_fixed_injected_error_and_all_passes_ran(converted):
    res, transport = converted
    h1 = res.questions[1].history
    assert any("Pass 2 round 1: corrected" in h for h in h1)
    assert any(h.startswith("Pass 3") for h in h1)
    tasks = [next(t["name"] for t in c["tools"] if t["name"].startswith("submit")) for c in transport.calls]
    assert tasks.count("submit_verification") >= 2 * len(EXPECTED["questions"])  # pass 2 + pass 3 for every question
    assert tasks.count("submit_figure_check") == 2


def test_final_validation_on_reloaded_file(converted, sample_pdf):
    res, _ = converted
    assert res.report.ok, res.report.format()
    from tmua_converter.pdf import PdfDocument
    with PdfDocument(sample_pdf) as doc:
        assert validate_paper(load_paper_dict(res.output_path), expected_questions=4, expected_duration=40,
                              pdf=doc).ok
    assert res.summary()["questions_with_images"] == [2, 3]


def test_images_are_real_crops(converted):
    res, _ = converted
    for n in (2, 3):
        fig = res.questions[n].figures[0]
        assert fig.confirmed
        assert fig.crop.metrics["edges_cutting_ink"] == []
        assert fig.crop.metrics["whitespace_fraction"] < 0.35
        assert fig.crop.metrics["page_height_fraction"] < 0.35


def test_non_converging_verification_flags_question(sample_pdf, tmp_path):
    res, _ = run(sample_pdf, tmp_path, Script(verifier_always_corrects={4}), max_verify_rounds=2,
                 max_pass3_rounds=1)
    data = load_paper_dict(res.output_path)
    q4 = data["questions"][3]
    assert q4["needsReview"] is True
    assert res.questions[4].review_reasons
    assert all(not q["needsReview"] for q in data["questions"][:3])


def test_overrides(sample_pdf, tmp_path):
    res, _ = run(sample_pdf, tmp_path, title="Hercules Set 2", paper="Paper 1", year="Set 2", duration_minutes=75,
                 filename="custom.tmua.json")
    data = load_paper_dict(res.output_path)
    assert res.output_path.name == "custom.tmua.json"
    assert (data["title"], data["year"], data["durationMinutes"]) == ("Hercules Set 2", "Set 2", 75)


def test_combine_and_naming(converted):
    res, _ = converted
    p = res.paper
    combined = combine_papers([p, p], "My Combined Paper")
    assert [q.number for q in combined.questions] == list(range(1, 9))
    assert combined.title == "My Combined Paper" and combined.durationMinutes == 80
    assert combined.questions[4].stem == p.questions[0].stem
    assert validate_paper(combined.model_dump(mode="json"), expected_questions=8).ok
    assert paper_filename("Hercules Set 2", "Paper 1") == "Hercules_Set_2_Paper_1.tmua.json"
    assert paper_filename("TMUA 2019 Paper 1", "Paper 1") == "TMUA_2019_Paper_1.tmua.json"
    assert paper_id("Hercules Set 2", "Paper 2") == "hercules-set-2-paper-2"
    Paper.model_validate(combined.model_dump())
