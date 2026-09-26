"""Local web app: upload PDFs, watch progress, review/edit, download .tmua.json.

Run with ``tmua-app`` (or ``python -m tmua_converter.web.app``) and open
http://127.0.0.1:8000.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import ValidationError

from ..browser import playwright_available
from ..ocr import find_tessdata
from ..pdf import Box, PdfDocument
from ..pipeline import ConvertOptions
from ..ocr import is_image_file
from ..schema import load_paper_dict
from ..service import save_edited_paper
from ..validator import validate_paper
from .jobs import JobManager

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 80 * 1024 * 1024


def _is_picture(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def create_app(workdir: Path | None = None) -> FastAPI:
    app = FastAPI(title="TMUA paper converter")
    root = Path(workdir or os.environ.get("TMUA_WORKDIR", "tmua_output")).resolve()
    jobs = JobManager(root)
    app.state.jobs = jobs

    docs: dict[str, PdfDocument] = {}

    def open_doc(path: Path) -> PdfDocument:
        """Documents stay open so OCR'd pages are not read again on every request."""
        key = str(path)
        if key not in docs:
            docs[key] = PdfDocument(path)
        return docs[key]

    def job_or_404(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    def paper_or_404(job_id: str, k: int):
        job = job_or_404(job_id)
        if not 0 <= k < len(job.papers) or job.papers[k].out_path is None:
            raise HTTPException(404, "paper not available")
        return job, job.papers[k]

    @app.get("/api/config")
    def config():
        return {"ocr": find_tessdata() is not None, "render_check": playwright_available(), "workdir": str(root)}

    @app.post("/api/jobs")
    async def create_job(files: list[UploadFile] = File(...), options: str = Form("{}")):
        try:
            opts = json.loads(options or "{}")
        except ValueError as exc:
            raise HTTPException(400, "options must be JSON") from exc
        uploads = []
        for f in files:
            data = await f.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"{f.filename} is too large")
            name = f.filename or "paper.pdf"
            if data.startswith(b"%PDF"):
                if not name.lower().endswith(".pdf"):
                    name += ".pdf"
            elif _is_picture(data):
                if not is_image_file(name):
                    name += ".png"
            else:
                raise HTTPException(400, f"{f.filename} is not a PDF or a picture")
            uploads.append((name, data))
        if not uploads:
            raise HTTPException(400, "no files uploaded")
        base = ConvertOptions(render_check=bool(opts.get("render_check", True)))
        per_paper = []
        for p in opts.get("papers") or []:
            per_paper.append({
                "title": (p.get("title") or "").strip() or None,
                "paper": (p.get("paper") or "").strip() or None,
                "year": (p.get("year") or "").strip() or None,
                "duration_minutes": int(p["duration"]) if str(p.get("duration") or "").strip() else None,
                "expected_questions": int(p["questions"]) if str(p.get("questions") or "").strip() else None,
            })
        combine = (opts.get("combine_title") or "").strip() or None
        pages = bool(opts.get("images_are_pages"))
        if combine and len(uploads) < 2:
            raise HTTPException(400, "combining needs at least two papers")
        job = jobs.start_conversion(uploads, base, per_paper=per_paper, combine_title=combine,
                                    images_are_one_paper=pages)
        return {"id": job.id}

    @app.post("/api/review")
    async def open_review(file: UploadFile = File(...), pdf: UploadFile | None = File(None)):
        data = await file.read()
        pdf_data = None
        if pdf is not None and pdf.filename:
            raw = await pdf.read()
            if not raw.startswith(b"%PDF"):
                raise HTTPException(400, "the source file is not a PDF")
            pdf_data = (pdf.filename, raw)
        try:
            job = jobs.open_for_review(file.filename or "paper.tmua.json", data, pdf_data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, f"not a valid JSON file: {exc}") from exc
        return {"id": job.id}

    @app.get("/api/jobs")
    def list_jobs():
        return [{"id": j.id, "kind": j.kind, "status": j.status, "created": j.created,
                 "papers": [p.to_dict() for p in j.papers]}
                for j in sorted(jobs.jobs.values(), key=lambda j: -j.created)]

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, log_from: int = 0):
        return job_or_404(job_id).to_dict(log_from)

    @app.get("/api/jobs/{job_id}/papers/{k}")
    def get_paper(job_id: str, k: int):
        job, entry = paper_or_404(job_id, k)
        data = load_paper_dict(entry.out_path)
        report = _validate(data, entry)
        return {"paper": data, "report": report.to_dict(), "detail": entry.detail, "file": entry.out_path.name,
                "has_pdf": entry.pdf_path is not None, "summary": entry.summary}

    @app.put("/api/jobs/{job_id}/papers/{k}")
    async def put_paper(job_id: str, k: int, request: Request):
        job, entry = paper_or_404(job_id, k)
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "body must be JSON") from exc
        # Validate the edited paper as it will be written *and* re-read.
        report = validate_paper(data)
        try:
            save_edited_paper(data, entry.out_path)
        except ValidationError as exc:
            return JSONResponse({"saved": False, "report": report.to_dict(),
                                 "schema_errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                   for e in exc.errors()]}, status_code=422)
        reloaded = load_paper_dict(entry.out_path)
        return {"saved": True, "paper": reloaded, "report": _validate(reloaded, entry).to_dict()}

    @app.get("/api/jobs/{job_id}/papers/{k}/source/{number}.png")
    def source_image(job_id: str, k: int, number: int):
        job, entry = paper_or_404(job_id, k)
        if entry.pdf_path is None:
            raise HTTPException(404, "no source PDF for this paper")
        data = load_paper_dict(entry.out_path)
        q = next((q for q in data.get("questions", []) if q.get("number") == number), None)
        if q is None:
            raise HTTPException(404, "no such question")
        doc = open_doc(entry.pdf_path)
        regions = [(int(p), Box(*b)) for p, b in entry.detail.get(str(number), {}).get("regions", [])]
        if not regions:
            regions = doc.question_regions(number)
        if not regions:
            page = int(q.get("sourcePage") or 1)
            page = min(max(page, 1), doc.page_count)
            regions = [(page, doc.page_box(page))]
        parts = [doc.render(p, 2.5, b) for p, b in regions]
        width = max(im.width for im in parts)
        out = Image.new("RGB", (width, sum(im.height for im in parts) + 12 * (len(parts) - 1)), "white")
        y = 0
        for im in parts:
            out.paste(im, (0, y))
            y += im.height + 12
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/jobs/{job_id}/papers/{k}/download")
    def download(job_id: str, k: int):
        job, entry = paper_or_404(job_id, k)
        return FileResponse(entry.out_path, media_type="application/json", filename=entry.out_path.name)

    @app.get("/api/jobs/{job_id}/papers/{k}/report")
    def download_report(job_id: str, k: int):
        job, entry = paper_or_404(job_id, k)
        from ..service import report_path_for
        rp = report_path_for(entry.out_path)
        if not rp.exists():
            raise HTTPException(404, "no report")
        return FileResponse(rp, media_type="application/json", filename=rp.name)

    @app.get("/api/jobs/{job_id}/combined/download")
    def download_combined(job_id: str):
        job = job_or_404(job_id)
        if job.combined_path is None:
            raise HTTPException(404, "no combined file")
        return FileResponse(job.combined_path, media_type="application/json", filename=job.combined_path.name)

    def _validate(data: dict, entry):
        if entry.pdf_path is not None:
            return validate_paper(data, pdf=open_doc(entry.pdf_path))
        return validate_paper(data)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="tmua-app", description="Run the TMUA paper converter web app.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--workdir", type=Path, default=None, help="where uploads and outputs are kept "
                                                               "(default ./tmua_output)")
    args = ap.parse_args(argv)
    print(f"TMUA paper converter running at http://{args.host}:{args.port}")
    uvicorn.run(create_app(args.workdir), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
