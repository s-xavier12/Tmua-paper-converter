"""Background conversion jobs for the web app."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pipeline import ConvertOptions
from ..service import convert_batch, make_runner, report_path_for

log = logging.getLogger(__name__)


def safe_name(name: str, default: str = "file") -> str:
    name = Path(name or default).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or default
    return name[:120]


@dataclass
class PaperEntry:
    index: int
    pdf_path: Path | None
    out_path: Path | None = None
    summary: dict | None = None
    detail: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "pdf": self.pdf_path.name if self.pdf_path else None,
                "file": self.out_path.name if self.out_path else None, "summary": self.summary, "error": self.error}


@dataclass
class Job:
    id: str
    workdir: Path
    kind: str  # "convert" | "review"
    status: str = "queued"  # queued | running | done | failed
    created: float = field(default_factory=time.time)
    log: list[dict] = field(default_factory=list)
    papers: list[PaperEntry] = field(default_factory=list)
    combine_title: str | None = None
    combined_path: Path | None = None
    combined_report: dict | None = None
    error: str | None = None

    def add_log(self, stage: str, msg: str) -> None:
        self.log.append({"t": round(time.time() - self.created, 1), "stage": stage, "msg": msg})

    def to_dict(self, log_from: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "error": self.error,
                "log": self.log[log_from:], "log_total": len(self.log),
                "papers": [p.to_dict() for p in self.papers], "combine_title": self.combine_title,
                "combined": self.combined_path.name if self.combined_path else None,
                "combined_report": self.combined_report}


class JobManager:
    def __init__(self, root: Path, transport_factory=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # tests inject a fake Claude transport here
        self.transport_factory = transport_factory

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _new_job(self, kind: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        workdir = self.root / job_id
        (workdir / "input").mkdir(parents=True, exist_ok=True)
        (workdir / "output").mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, workdir=workdir, kind=kind)
        with self._lock:
            self.jobs[job_id] = job
        return job

    # ------------------------------------------------------------------ convert
    def start_conversion(self, files: list[tuple[str, bytes]], options: ConvertOptions,
                         per_paper: list[dict] | None = None, combine_title: str | None = None,
                         api_key: str | None = None) -> Job:
        job = self._new_job("convert")
        job.combine_title = combine_title or None
        pdfs = []
        for i, (name, data) in enumerate(files, start=1):
            path = job.workdir / "input" / f"{i:02d}_{safe_name(name, 'paper.pdf')}"
            path.write_bytes(data)
            pdfs.append(path)
            job.papers.append(PaperEntry(index=i - 1, pdf_path=path))
        per_opts = None
        if per_paper:
            per_opts = []
            for i in range(len(pdfs)):
                o = per_paper[i] if i < len(per_paper) else {}
                per_opts.append(ConvertOptions(**{**options.__dict__, **{k: v for k, v in o.items() if v not in
                                                                        (None, "")}}))
        thread = threading.Thread(target=self._run, args=(job, pdfs, options, per_opts, api_key), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, pdfs: list[Path], options: ConvertOptions, per_opts, api_key: str | None) -> None:
        job.status = "running"
        job.add_log("start", f"Converting {len(pdfs)} paper(s) with {options.model} (effort {options.effort})")
        try:
            transport = self.transport_factory() if self.transport_factory else None
            runner = make_runner(options, api_key=api_key, transport=transport)
            batch = convert_batch(pdfs, job.workdir / "output", options, runner, combine_title=job.combine_title,
                                  progress=job.add_log, per_paper_options=per_opts)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.add_log("error", job.error)
            return
        # convert_batch processes PDFs in order: successes arrive in order,
        # failures are reported by PDF name.
        results = list(batch.results)
        failures = dict(batch.failures)
        for entry in job.papers:
            name = entry.pdf_path.name
            if name in failures:
                entry.error = failures[name]
                continue
            if results:
                r = results.pop(0)
                entry.out_path = r.output_path
                entry.summary = r.summary()
                entry.detail = _load_detail(r.output_path)
        job.combined_path = batch.combined_path
        if batch.combined_report is not None:
            job.combined_report = batch.combined_report.to_dict()
        job.status = "done" if not batch.failures else ("failed" if not batch.results else "done")
        if batch.failures:
            job.error = "; ".join(f"{n}: {e}" for n, e in batch.failures)
        job.add_log("done", "All done" if not batch.failures else "Finished with failures")

    # ------------------------------------------------------------------ review existing file
    def open_for_review(self, json_name: str, json_bytes: bytes, pdf: tuple[str, bytes] | None) -> Job:
        job = self._new_job("review")
        out = job.workdir / "output" / safe_name(json_name, "paper.tmua.json")
        if not out.name.endswith(".json"):
            out = out.with_suffix(".json")
        data = json.loads(json_bytes.decode("utf-8"))  # raises on invalid JSON
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pdf_path = None
        if pdf is not None:
            pdf_path = job.workdir / "input" / safe_name(pdf[0], "paper.pdf")
            pdf_path.write_bytes(pdf[1])
        job.papers.append(PaperEntry(index=0, pdf_path=pdf_path, out_path=out))
        job.status = "done"
        job.add_log("review", f"Opened {out.name} for review")
        return job


def _load_detail(out_path: Path | None) -> dict[str, Any]:
    if out_path is None:
        return {}
    rp = report_path_for(out_path)
    if not rp.exists():
        return {}
    try:
        return json.loads(rp.read_text(encoding="utf-8")).get("questions_detail", {})
    except (OSError, ValueError):
        return {}
