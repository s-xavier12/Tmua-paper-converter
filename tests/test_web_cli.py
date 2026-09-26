import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tmua_converter import cli, service

LATEX = Path(__file__).parent / "data" / "latex_cm.pdf"


@pytest.fixture()
def client(tmp_path):
    from tmua_converter.web.app import create_app

    with TestClient(create_app(tmp_path / "work")) as c:
        yield c


def wait_done(client, job_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.5)
    raise AssertionError("job did not finish")


def test_web_convert_review_edit_download(client):
    opts = {"render_check": False, "papers": [{"duration": ""}]}
    r = client.post("/api/jobs", files=[("files", ("sample.pdf", LATEX.read_bytes(), "application/pdf"))],
                    data={"options": json.dumps(opts)})
    assert r.status_code == 200, r.text
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "done", job
    s = job["papers"][0]["summary"]
    assert s["questions"] == 8 and s["validation"]["ok"] and s["questions_with_images"] == [2, 3, 8]
    assert s["needs_review"] == {}

    base = f"/api/jobs/{job['id']}/papers/0"
    got = client.get(base).json()
    assert got["report"]["ok"] and got["has_pdf"]
    src = client.get(f"{base}/source/2.png")
    assert src.status_code == 200 and src.content.startswith(b"\x89PNG")

    paper = got["paper"]
    paper["questions"][0]["stem"] = "Broken $\\frac{1}{2$"
    r = client.put(base, json=paper)
    assert r.status_code == 200 and r.json()["saved"] and not r.json()["report"]["ok"]
    paper["questions"][0]["correctAnswer"] = "A"
    assert client.put(base, json=paper).status_code == 422  # schema forbids extra keys

    dl = client.get(f"{base}/download")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    assert json.loads(dl.content)["questions"][0]["stem"] == "Broken $\\frac{1}{2$"
    assert client.get(f"{base}/report").status_code == 200


def test_web_rejects_non_pdf(client):
    r = client.post("/api/jobs", files=[("files", ("x.pdf", b"hello", "application/pdf"))], data={"options": "{}"})
    assert r.status_code == 400


def test_web_config_has_no_ai_settings(client):
    cfg = client.get("/api/config").json()
    assert set(cfg) == {"ocr", "render_check", "workdir"}


def test_web_review_existing_file(client, sample_pdf):
    from test_validator import good_paper

    with open(sample_pdf, "rb") as fh:
        r = client.post("/api/review", files={"file": ("mine.tmua.json", json.dumps(good_paper()).encode()),
                                              "pdf": ("s.pdf", fh.read(), "application/pdf")})
    assert r.status_code == 200
    got = client.get(f"/api/jobs/{r.json()['id']}/papers/0").json()
    assert got["report"]["ok"]
    assert client.get(f"/api/jobs/{r.json()['id']}/papers/0/source/1.png").status_code == 200


def test_cli_validate(tmp_path, capsys):
    from test_validator import good_paper

    good = tmp_path / "good.tmua.json"
    good.write_text(json.dumps(good_paper()))
    assert cli.validate_main([str(good), "--questions", "2"]) == 0
    bad_data = good_paper()
    bad_data["questions"][0]["stem"] = "literal \\n newline"
    bad = tmp_path / "bad.tmua.json"
    bad.write_text(json.dumps(bad_data))
    assert cli.validate_main([str(bad)]) == 1
    assert "literal-backslash-n" in capsys.readouterr().out


def test_cli_convert_and_combine(tmp_path, capsys):
    a, b = tmp_path / "first.pdf", tmp_path / "second.pdf"
    shutil.copy(LATEX, a)
    shutil.copy(LATEX, b)
    status = cli.convert_main([str(a), str(b), "-o", str(tmp_path / "out"), "--combine", "Mock Exam 1",
                               "--no-render-check"])
    out = capsys.readouterr().out
    assert status == 0, out
    assert "Conversion completed" in out and "Questions with source images: 2, 3, 8" in out
    assert "Needs review: none" in out
    combined = json.loads((tmp_path / "out" / "Mock_Exam_1.tmua.json").read_text())
    assert combined["title"] == "Mock Exam 1" and len(combined["questions"]) == 16
    assert [q["number"] for q in combined["questions"]] == list(range(1, 17))
    assert combined["durationMinutes"] == 150
    names = sorted(p.name for p in (tmp_path / "out").glob("*.tmua.json"))
    assert names == ["Mock_Exam_1.tmua.json", "Sample_Admissions_Test_Paper_1.tmua.json",
                     "Sample_Admissions_Test_Paper_1_2.tmua.json"]  # same title twice: no overwrite
    assert service.report_path_for(tmp_path / "out" / "x.tmua.json").name == "x.report.json"


def test_pictures_as_pages_of_one_paper(tmp_path):
    import pymupdf

    doc = pymupdf.open(LATEX)
    pics = []
    for i in range(3):
        p = tmp_path / f"page{i + 1}.png"
        doc[i].get_pixmap(dpi=50).save(p)
        pics.append(p)
    grouped = service.group_inputs(pics, True, tmp_path / "work")
    assert len(grouped) == 1 and pymupdf.open(grouped[0]).page_count == 3
    assert service.group_inputs(pics, False, tmp_path / "work") == pics
