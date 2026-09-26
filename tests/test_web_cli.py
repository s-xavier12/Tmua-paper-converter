import json
import shutil
import time

import pytest
from conftest import CHROMIUM
from fake_claude import FakeTransport
from fastapi.testclient import TestClient

from tmua_converter import cli, service
from tmua_converter.llm import ClaudeRunner


@pytest.fixture()
def client(sample_pdf, tmp_path):
    from tmua_converter.web.app import create_app

    app = create_app(tmp_path / "work", transport_factory=lambda: FakeTransport(sample_pdf))
    with TestClient(app) as c:
        yield c


def wait_done(client, job_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.5)
    raise AssertionError("job did not finish")


def test_web_convert_review_edit_download(client, sample_pdf):
    opts = {"effort": "high", "render_check": CHROMIUM, "papers": [{"duration": ""}]}
    with open(sample_pdf, "rb") as fh:
        r = client.post("/api/jobs", files=[("files", ("sample.pdf", fh.read(), "application/pdf"))],
                        data={"options": json.dumps(opts)})
    assert r.status_code == 200, r.text
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "done", job
    s = job["papers"][0]["summary"]
    assert s["questions"] == 4 and s["validation"]["ok"] and s["questions_with_images"] == [2, 3]

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


def test_web_review_existing_file(client, sample_pdf, tmp_path):
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


def test_cli_convert_and_combine(sample_pdf, tmp_path, monkeypatch, capsys):
    pdf2 = tmp_path / "second.pdf"
    shutil.copy(sample_pdf, pdf2)
    monkeypatch.setattr(cli, "make_runner", lambda opts: ClaudeRunner(FakeTransport(sample_pdf), model=opts.model))
    args = [str(sample_pdf), str(pdf2), "-o", str(tmp_path / "out"), "--combine", "Mock Exam 1"]
    if not CHROMIUM:
        args.append("--no-render-check")
    status = cli.convert_main(args)
    out = capsys.readouterr().out
    assert status == 0, out
    assert "Conversion completed" in out and "Questions with source images: 2, 3" in out
    combined = json.loads((tmp_path / "out" / "Mock_Exam_1.tmua.json").read_text())
    assert combined["title"] == "Mock Exam 1" and len(combined["questions"]) == 8
    assert [q["number"] for q in combined["questions"]] == list(range(1, 9))
    assert service.report_path_for(tmp_path / "out" / "x.tmua.json").name == "x.report.json"
