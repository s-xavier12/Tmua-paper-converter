"""The conversion pipeline.

    metadata  ->  pass 1: transcribe each page (Claude, with zoom + crop tools)
              ->  crop every figure from the PDF and have Claude check each crop
              ->  pass 2: independent verification of every question against the
                  source (and against a KaTeX render of the JSON), repeated until
                  a fresh check finds nothing to correct
              ->  write the file, reload it with a JSON parser, validate it
              ->  pass 3: re-check every question *from the reloaded file*
              ->  write, reload and validate again: that report is the one shown.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import prompts
from .figures import CropResult, crop_figure, overlay_box
from .llm import ClaudeRunner, SubmitRejected, Task, TaskFailed, Tool, ToolResult, image_block, text_block
from .naming import paper_filename, paper_id
from .pdf import Box, PdfDocument
from .render import QuestionRenderer, try_start_renderer
from .schema import Image, Option, Paper, Question, load_paper_dict, write_paper
from .validator import ValidationReport, check_string, validate_paper

log = logging.getLogger(__name__)

Progress = Callable[[str, str], None]

FIGURE_KINDS = ["diagram", "graph", "answer_options_panel", "table", "other"]

_BOX_PROPS = {k: {"type": "number"} for k in ("x0", "y0", "x1", "y1")}
FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "description": "PDF page number the figure is printed on"},
        **_BOX_PROPS,
        "kind": {"type": "string", "enum": FIGURE_KINDS},
        "alt": {"type": "string", "description": "Factual description of what the figure shows (no answers)"},
    },
    "required": ["page", "x0", "y0", "x1", "y1", "kind", "alt"],
    "additionalProperties": False,
}
OPTION_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "content": {"type": "string"}},
    "required": ["label", "content"],
    "additionalProperties": False,
}
REGION_SCHEMA = {
    "type": "object",
    "description": "Vertical extent of the whole question (number to last option) in page-image pixels. If it "
                   "continues onto the next page, continuation_y1 is where it ends on that page, else 0.",
    "properties": {"y0": {"type": "number"}, "y1": {"type": "number"},
                   "continues_on_next_page": {"type": "boolean"}, "continuation_y1": {"type": "number"}},
    "required": ["y0", "y1", "continues_on_next_page", "continuation_y1"],
    "additionalProperties": False,
}


# ----------------------------------------------------------------------------- data
@dataclass
class ConvertOptions:
    model: str = "claude-opus-5"
    effort: str = "high"
    concurrency: int = 4
    render_check: bool = True
    max_verify_rounds: int = 3
    max_pass3_rounds: int = 2
    max_figure_attempts: int = 3
    title: str | None = None
    paper: str | None = None
    year: str | None = None
    duration_minutes: int | None = None
    expected_questions: int | None = None
    filename: str | None = None


@dataclass
class FigureSpec:
    page: int
    box_pt: Box
    kind: str
    alt: str
    exact: bool = False


@dataclass
class FinalFigure:
    spec: FigureSpec
    crop: CropResult
    alt: str
    confirmed: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class QuestionState:
    number: int
    stem: str
    options: list[dict]
    source_page: int
    figure_specs: list[FigureSpec] = field(default_factory=list)
    figures: list[FinalFigure] = field(default_factory=list)
    regions: list[tuple[int, Box]] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    def flag(self, reason: str) -> None:
        self.needs_review = True
        if reason not in self.review_reasons:
            self.review_reasons.append(reason)

    def to_question(self) -> Question:
        return Question(
            number=self.number,
            stem=self.stem,
            options=[Option(label=o["label"], content=o["content"]) for o in self.options],
            images=[Image(src=f.crop.data_uri, alt=f.alt) for f in self.figures],
            sourcePage=self.source_page,
            needsReview=self.needs_review,
        )


@dataclass
class ConversionResult:
    paper: Paper
    output_path: Path | None
    report: ValidationReport
    metadata: dict
    questions: dict[int, QuestionState]
    usage: dict
    render_check_used: bool
    notes: list[str]

    def summary(self) -> dict:
        return {
            "file": str(self.output_path) if self.output_path else None,
            "title": self.paper.title,
            "questions": len(self.paper.questions),
            "durationMinutes": self.paper.durationMinutes,
            "questions_with_images": [q.number for q in self.paper.questions if q.images],
            "needs_review": {n: s.review_reasons for n, s in sorted(self.questions.items()) if s.needs_review},
            "validation": self.report.to_dict(),
            "render_check_used": self.render_check_used,
            "usage": self.usage,
            "notes": self.notes,
        }


# ----------------------------------------------------------------------------- converter
class Converter:
    def __init__(self, runner: ClaudeRunner, options: ConvertOptions | None = None,
                 progress: Progress | None = None, renderer: QuestionRenderer | None = None):
        self.runner = runner
        self.opt = options or ConvertOptions()
        self._progress = progress or (lambda stage, msg: None)
        self._renderer = renderer
        self._own_renderer = False

    def progress(self, stage: str, msg: str) -> None:
        log.info("[%s] %s", stage, msg)
        self._progress(stage, msg)

    # ======================================================================= entry point
    def convert(self, pdf_path: str | Path, out_dir: str | Path | None = None) -> ConversionResult:
        pdf_path = Path(pdf_path)
        self.doc = PdfDocument(pdf_path)
        self.notes: list[str] = []
        try:
            if self._renderer is None and self.opt.render_check:
                self.progress("setup", "Starting headless KaTeX renderer")
                self._renderer = try_start_renderer()
                self._own_renderer = self._renderer is not None
                if self._renderer is None:
                    self.notes.append("Rendered-preview check unavailable (install playwright + Chromium to "
                                      "enable it); verification compared LaTeX source with the page images.")
            with ThreadPoolExecutor(max_workers=max(1, self.opt.concurrency)) as pool:
                self.pool = pool
                return self._convert(pdf_path, Path(out_dir) if out_dir else pdf_path.parent)
        finally:
            if self._own_renderer and self._renderer is not None:
                self._renderer.close()
                self._renderer = None
            self.doc.close()

    def _convert(self, pdf_path: Path, out_dir: Path) -> ConversionResult:
        doc = self.doc
        self.progress("metadata", f"Reading cover page of {pdf_path.name} ({doc.page_count} pages)")
        meta = self._metadata(pdf_path.name)

        # ---------------------------------------------------------------- pass 1
        pages = [p for p in range(1, doc.page_count + 1) if not doc.is_blank_page(p)]
        self.progress("pass1", f"Pass 1: transcribing {len(pages)} pages")
        drafts: dict[int, QuestionState] = {}
        results = self._map(lambda p: (p, self._transcribe_page(p)), pages, "pass1", "page")
        for p, qs in sorted(results, key=lambda r: r[0]):
            for q in qs:
                self._merge_draft(drafts, q)

        expected = self.opt.expected_questions or meta.get("stated_question_count") or None
        drafts = self._recover_missing(drafts, expected)
        if not drafts:
            raise TaskFailed("no questions were found in the PDF")
        anchors = doc.find_question_anchors()
        for n, q in drafts.items():
            if n in anchors and anchors[n].page != q.source_page:
                q.history.append(f"sourcePage {q.source_page} -> {anchors[n].page} (question number printed there)")
                q.source_page = anchors[n].page
            anchor_regions = doc.question_regions(n)
            if anchor_regions:
                q.regions = anchor_regions

        # ---------------------------------------------------------------- figures
        self._crop_all_figures(drafts)

        # ---------------------------------------------------------------- pass 2
        self._verify_rounds(drafts, "Pass 2", self.opt.max_verify_rounds, final_round_flags=True)

        # ---------------------------------------------------------------- write, reload, validate, pass 3
        title, paper_name, year, duration = self._final_metadata(meta)
        name = self.opt.filename or paper_filename(title, paper_name)
        out_path = out_dir / name
        build = lambda: Paper(id=paper_id(title, paper_name), title=title, paper=paper_name, year=year,  # noqa: E731
                              durationMinutes=duration,
                              questions=[drafts[n].to_question() for n in sorted(drafts)])
        write_paper(build(), out_path)
        self.progress("pass3", f"Wrote {out_path.name}; reloading it with a JSON parser")

        pass3_targets: list[int] = []
        for rnd in range(1, self.opt.max_pass3_rounds + 1):
            data = load_paper_dict(out_path)
            report = self._validate(data, expected, meta)
            targets = sorted(drafts) if rnd == 1 else pass3_targets
            self.progress("pass3", f"Pass 3 (round {rnd}): re-checking {len(targets)} questions from the "
                                   "reloaded file")
            changed = self._pass3(drafts, data, report, targets, rnd)
            if not changed:
                break
            pass3_targets = changed
            write_paper(build(), out_path)
        else:
            for n in pass3_targets:
                drafts[n].flag("final-file check still found differences after corrections")

        # Anything the validator still rejects is flagged for manual review.
        data = load_paper_dict(out_path)
        report = self._validate(data, expected, meta)
        for issue in report.errors:
            if issue.question in drafts:
                drafts[issue.question].flag(f"validator: {issue.message}")
        write_paper(build(), out_path)

        # The delivered file: reload and validate once more.
        data = load_paper_dict(out_path)
        report = self._validate(data, expected, meta)
        paper = Paper.model_validate(data)
        self.progress("done", f"Finished: {len(paper.questions)} questions, "
                              f"{len(report.errors)} validation errors, "
                              f"{sum(q.needsReview for q in paper.questions)} flagged for review")
        return ConversionResult(paper=paper, output_path=out_path, report=report, metadata=meta, questions=drafts,
                                usage=self.runner.usage.to_dict(self.runner.model),
                                render_check_used=self._renderer is not None, notes=self.notes)

    # ======================================================================= helpers
    def _map(self, fn: Callable[[Any], Any], items: list, stage: str, noun: str) -> list:
        """Run *fn* over items on the pool, reporting progress; failures are logged and skipped."""
        futures = [(item, self.pool.submit(fn, item)) for item in items]
        out = []
        for i, (item, fut) in enumerate(futures, start=1):
            try:
                out.append(fut.result())
            except TaskFailed as exc:
                self.notes.append(str(exc))
                log.error("%s", exc)
            self.progress(stage, f"{noun} {i}/{len(items)} done")
        return out

    def _page_image(self, page: int):
        return self.doc.render_model_page(page)

    # -- tools ---------------------------------------------------------------
    def _box_from_input(self, inp: dict) -> tuple[int, Box]:
        page = int(inp["page"])
        if not 1 <= page <= self.doc.page_count:
            raise ValueError(f"page must be between 1 and {self.doc.page_count}")
        box = Box(float(inp["x0"]), float(inp["y0"]), float(inp["x1"]), float(inp["y1"]))
        w, h = self.doc.model_image_size(page)
        box = box.clamp(Box(0, 0, w, h))
        if box.width < 6 or box.height < 6:
            raise ValueError("region is empty or too small (coordinates are page-image pixels: x0 < x1, y0 < y1)")
        return page, box

    def _view_region_tool(self) -> Tool:
        def handler(inp: dict) -> ToolResult:
            page, box = self._box_from_input(inp)
            img = self.doc.render_region_for_model(page, self.doc.px_to_pt(page, box), max_zoom=10.0)
            scale = img.width / box.width
            return ToolResult([text_block(f"Page {page}, region {box.to_list(0)} px, shown at {scale:.1f}x the "
                                          "page-image scale."), image_block(img)])

        return Tool(
            name="view_region",
            description="Zoom into a rectangular region of a PDF page at high resolution. Use it whenever small "
                        "details matter: exponents, subscripts, limits, fraction bars, roots, inequality signs, "
                        "labels. Coordinates are page-image pixels [x0, y0, x1, y1].",
            input_schema={"type": "object", "properties": {"page": {"type": "integer"}, **_BOX_PROPS},
                          "required": ["page", "x0", "y0", "x1", "y1"], "additionalProperties": False},
            handler=handler,
        )

    def _preview_crop_tool(self) -> Tool:
        def handler(inp: dict) -> ToolResult:
            page, box = self._box_from_input(inp)
            crop = crop_figure(self.doc, page, self.doc.px_to_pt(page, box), refine=not inp.get("exact", False))
            final_px = self.doc.pt_to_px(page, crop.box_pt)
            info = (f"Crop of page {page}. Requested {box.to_list(0)} px; final crop {final_px.to_list(0)} px "
                    f"after automatic tightening; image {crop.image.width}x{crop.image.height} px.\n"
                    f"Adjustments: {'; '.join(crop.notes) or 'none'}\nMetrics: {crop.metrics}")
            return ToolResult([text_block(info), image_block(crop.image)])

        return Tool(
            name="preview_figure_crop",
            description="Preview exactly how a figure will be cropped from the PDF. The box is automatically "
                        "snapped to the drawing, extended to include its labels and trimmed of whitespace; set "
                        "exact=true to use your box as given (only trimmed). Use it to confirm the crop contains "
                        "the whole figure and nothing else.",
            input_schema={"type": "object",
                          "properties": {"page": {"type": "integer"}, **_BOX_PROPS, "exact": {"type": "boolean"}},
                          "required": ["page", "x0", "y0", "x1", "y1", "exact"], "additionalProperties": False},
            handler=handler,
        )

    # -- metadata --------------------------------------------------------------
    def _metadata(self, filename: str) -> dict:
        doc = self.doc
        pages = [p for p in range(1, min(doc.page_count, 2) + 1)]
        content: list[dict] = []
        for p in pages:
            w, h = doc.model_image_size(p)
            content += [text_block(f"PDF page {p} ({w}x{h} px):"), image_block(self._page_image(p))]
        text_hint = "\n".join(f"--- page {p} ---\n{doc.text(p)[:3000]}" for p in range(1, min(doc.page_count, 3) + 1))
        content.append(text_block(prompts.METADATA_TASK.format(filename=filename)
                                  + (f"\nText layer of the first pages (hint only):\n{text_hint}" if text_hint.strip()
                                     else "")))

        def validate(inp: dict) -> dict:
            if not str(inp.get("title", "")).strip():
                raise SubmitRejected("title must not be empty")
            if int(inp.get("duration_minutes", 0)) < 0 or int(inp.get("stated_question_count", 0)) < 0:
                raise SubmitRejected("numbers must not be negative")
            return dict(inp)

        schema = {"type": "object", "properties": {
            "title": {"type": "string"}, "paper": {"type": "string"}, "year": {"type": "string"},
            "duration_minutes": {"type": "integer"}, "duration_evidence": {"type": "string"},
            "stated_question_count": {"type": "integer"}, "count_evidence": {"type": "string"}},
            "required": ["title", "paper", "year", "duration_minutes", "duration_evidence",
                         "stated_question_count", "count_evidence"], "additionalProperties": False}
        task = Task(name="metadata", system=prompts.SYSTEM_PROMPT, content=content, tools=[self._view_region_tool()],
                    submit=Tool("submit_metadata", "Submit the paper's metadata.", schema, validate=validate),
                    max_turns=8)
        meta = self.runner.run(task)
        self.progress("metadata", f"Title: {meta['title']!r}; duration: {meta['duration_minutes'] or 'not stated'}; "
                                  f"stated questions: {meta['stated_question_count'] or 'not stated'}")
        return meta

    def _final_metadata(self, meta: dict) -> tuple[str, str, str, int]:
        title = self.opt.title or meta["title"].strip()
        paper_name = self.opt.paper if self.opt.paper is not None else meta.get("paper", "").strip()
        year = self.opt.year or meta.get("year", "").strip() or "Unknown"
        duration = self.opt.duration_minutes or int(meta.get("duration_minutes") or 0)
        if not duration:
            duration = 75
            self.notes.append("The PDF does not state a time allowed, so durationMinutes was set to 75 (the TMUA "
                              "standard). Change it with --duration or on the review page if that is wrong.")
        if not paper_name:
            paper_name = "Paper 1"
            self.notes.append("The PDF does not name the paper, so the 'paper' field was set to 'Paper 1'.")
        return title, paper_name, year, duration

    # -- pass 1 ----------------------------------------------------------------
    def _transcribe_page(self, page: int, only_question: int | None = None) -> list[QuestionState]:
        doc = self.doc
        nxt = page + 1 if page < doc.page_count and not doc.is_blank_page(page + 1) else None
        size = doc.model_image_size(page)
        content: list[dict] = [text_block(f"Image 1 - PDF page {page}:"), image_block(self._page_image(page))]
        next_size = None
        if nxt is not None:
            next_size = doc.model_image_size(nxt)
            content += [text_block(f"Image 2 - PDF page {nxt} (continuations only):"), image_block(self._page_image(nxt))]
        anchors = [a for a in doc.find_question_anchors().values() if a.page == page]
        marker_hint = ""
        if anchors:
            z = doc.model_zoom(page)
            marker_hint = ("\nQuestion numbers detected in the PDF text layer on this page (may be incomplete): "
                           + ", ".join(f"{a.number} at y≈{a.box.y0 * z:.0f}px" for a in anchors) + "\n")
        page_text = doc.text(page).strip()
        text_hint = (f"\nPDF text layer for page {page} (hint only - maths in it is often scrambled):\n<text_layer>\n"
                     f"{page_text[:6000]}\n</text_layer>\n") if page_text else ""
        extra = ""
        if only_question is not None:
            extra = (f"\nNote: question {only_question} was not found in an earlier pass although it should start on "
                     f"this page. Submit ONLY question {only_question} (or an empty list if it is really not here).\n")
        content.append(text_block(prompts.PAGE_TASK.format(
            page=page, page_count=doc.page_count, next_page=nxt or "(none)",
            image_list=prompts.page_image_list(page, size, nxt, next_size),
            marker_hint=marker_hint, text_hint=text_hint, extra=extra)))

        attempts = {"n": 0}
        allowed_pages = {page} | ({nxt} if nxt else set())

        def validate(inp: dict) -> list[QuestionState]:
            attempts["n"] += 1
            problems: list[str] = []
            out: list[QuestionState] = []
            seen: set[int] = set()
            for i, q in enumerate(inp.get("questions", [])):
                num = q.get("number")
                where = f"question {num}"
                if not isinstance(num, int) or num < 1:
                    problems.append(f"questions[{i}]: number must be a positive integer")
                    continue
                if num in seen:
                    problems.append(f"{where}: submitted twice")
                seen.add(num)
                if only_question is not None and num != only_question:
                    problems.append(f"{where}: only question {only_question} was requested")
                problems += _content_problems(where, q.get("stem"), q.get("options"))
                specs = []
                for j, f in enumerate(q.get("figures", [])):
                    try:
                        fp, fbox = self._box_from_input(f)
                    except (ValueError, KeyError, TypeError) as exc:
                        problems.append(f"{where} figure {j + 1}: {exc}")
                        continue
                    if fp not in allowed_pages:
                        problems.append(f"{where} figure {j + 1}: page must be one of {sorted(allowed_pages)}")
                        continue
                    specs.append(FigureSpec(fp, doc.px_to_pt(fp, fbox), f.get("kind", "diagram"),
                                            f.get("alt", "").strip()))
                st = QuestionState(number=num, stem=q["stem"], options=[dict(o) for o in q["options"]],
                                   source_page=page, figure_specs=specs, regions=self._regions_from(page, nxt, q))
                if q.get("needs_review"):
                    st.flag(f"pass 1: {q.get('review_note') or 'flagged by transcriber'}")
                out.append(st)
            if problems and attempts["n"] < 3:
                raise SubmitRejected("\n".join(problems))
            for p in problems:  # accepted on the last attempt: carry the problems as review flags
                num = _leading_number(p)
                for st in out:
                    if st.number == num:
                        st.flag(f"pass 1 submission problem: {p}")
            return out

        schema = {"type": "object", "properties": {
            "questions": {"type": "array", "items": {"type": "object", "properties": {
                "number": {"type": "integer"},
                "stem": {"type": "string"},
                "options": {"type": "array", "items": OPTION_SCHEMA},
                "figures": {"type": "array", "items": FIGURE_SCHEMA},
                "region": REGION_SCHEMA,
                "needs_review": {"type": "boolean"},
                "review_note": {"type": "string"}},
                "required": ["number", "stem", "options", "figures", "region", "needs_review", "review_note"],
                "additionalProperties": False}},
            "page_note": {"type": "string"}},
            "required": ["questions", "page_note"], "additionalProperties": False}
        task = Task(name=f"pass1-page{page}", system=prompts.SYSTEM_PROMPT, content=content,
                    tools=[self._view_region_tool(), self._preview_crop_tool()],
                    submit=Tool("submit_page", "Submit the transcription of every question whose number is printed "
                                               "on this page (empty list if none).", schema, validate=validate),
                    max_turns=40)
        return self.runner.run(task)

    def _regions_from(self, page: int, nxt: int | None, q: dict) -> list[tuple[int, Box]]:
        r = q.get("region") or {}
        try:
            w, h = self.doc.model_image_size(page)
            y0, y1 = max(0.0, float(r["y0"])), min(float(h), float(r["y1"]))
            regions = []
            if y1 - y0 > 10:
                regions.append((page, self.doc.px_to_pt(page, Box(0, y0, w, y1))))
            if r.get("continues_on_next_page") and nxt is not None and float(r.get("continuation_y1", 0)) > 10:
                w2, h2 = self.doc.model_image_size(nxt)
                regions.append((nxt, self.doc.px_to_pt(nxt, Box(0, 0, w2, min(float(h2), float(r["continuation_y1"]))))))
            return regions
        except (KeyError, TypeError, ValueError):
            return []

    def _merge_draft(self, drafts: dict[int, QuestionState], q: QuestionState) -> None:
        existing = drafts.get(q.number)
        if existing is None:
            drafts[q.number] = q
            return
        anchors = self.doc.find_question_anchors()
        anchor_page = anchors[q.number].page if q.number in anchors else None
        keep, drop = existing, q
        if anchor_page is not None and q.source_page == anchor_page and existing.source_page != anchor_page:
            keep, drop = q, existing
        elif anchor_page is None and q.source_page < existing.source_page:
            keep, drop = q, existing
        keep.history.append(f"also transcribed from page {drop.source_page}; kept the page {keep.source_page} version")
        drafts[q.number] = keep

    def _recover_missing(self, drafts: dict[int, QuestionState], expected: int | None) -> dict[int, QuestionState]:
        anchors = self.doc.find_question_anchors()
        top = max([expected or 0, len(anchors), *drafts.keys()] or [0])
        missing = [n for n in range(1, top + 1) if n not in drafts]
        if not missing:
            return drafts
        self.progress("pass1", f"Questions {missing} missing after pass 1 - re-reading their pages")
        jobs = []
        for n in missing:
            if n in anchors:
                page = anchors[n].page
            else:
                before = [drafts[k].source_page for k in drafts if k < n]
                after = [drafts[k].source_page for k in drafts if k > n]
                lo = max(before) if before else 1
                hi = min(after) if after else self.doc.page_count
                page = next((p for p in range(lo, hi + 1) if not self.doc.is_blank_page(p)), lo)
            jobs.append((n, page))
        results = self._map(lambda job: self._transcribe_page(job[1], only_question=job[0]), jobs, "pass1",
                            "missing question")
        for qs in results:
            for q in qs:
                q.history.append("recovered in a targeted re-read of its page")
                self._merge_draft(drafts, q)
        return dict(sorted(drafts.items()))

    # -- figures ---------------------------------------------------------------
    def _crop_all_figures(self, drafts: dict[int, QuestionState]) -> None:
        jobs = [(q, spec) for q in drafts.values() for spec in q.figure_specs]
        if not jobs:
            self.progress("figures", "No figures to crop")
            return
        self.progress("figures", f"Cropping and checking {len(jobs)} figures")
        results = self._map(lambda job: (job[0].number, job[1], self._finalize_figure(job[0], job[1])), jobs,
                            "figures", "figure")
        by_q: dict[int, list[FinalFigure]] = {}
        for num, spec, fig in results:
            by_q.setdefault(num, []).append(fig)
        for q in drafts.values():
            figs = by_q.get(q.number, [])
            order = {id(s): i for i, s in enumerate(q.figure_specs)}
            q.figures = sorted(figs, key=lambda f: order.get(id(f.spec), 0))
            for f in q.figures:
                if not f.confirmed:
                    q.flag(f"image crop could not be confirmed: {'; '.join(f.notes) or 'see crop'}")
            if len(q.figures) < len(q.figure_specs):
                q.flag("a figure could not be cropped")

    def _finalize_figure(self, q: QuestionState, spec: FigureSpec, context: str = "") -> FinalFigure:
        doc = self.doc
        current = spec
        notes: list[str] = []
        crop = None
        alt = spec.alt
        for attempt in range(1, self.opt.max_figure_attempts + 1):
            crop = crop_figure(doc, current.page, current.box_pt, refine=not current.exact)
            verdict = self._check_figure(q, current, crop, alt, context)
            alt = verdict["alt"].strip() or alt
            if verdict["ok"]:
                return FinalFigure(current, crop, alt, True, notes)
            notes.append(f"attempt {attempt}: {', '.join(verdict['problems']) or 'problem'} - {verdict['note']}")
            cb = verdict["corrected_box"]
            page, box_px = self._box_from_input(cb)
            new = FigureSpec(page, doc.px_to_pt(page, box_px), current.kind, alt, bool(cb.get("exact")))
            if new.page == current.page and _same_box(new.box_pt, current.box_pt) and new.exact == current.exact:
                break
            current = new
        # last candidate after the final correction, unconfirmed
        crop = crop_figure(doc, current.page, current.box_pt, refine=not current.exact)
        return FinalFigure(current, crop, alt, False, notes)

    def _check_figure(self, q: QuestionState, spec: FigureSpec, crop: CropResult, alt: str, context: str) -> dict:
        doc = self.doc
        page_img = self._page_image(spec.page)
        box_px = doc.pt_to_px(spec.page, crop.box_pt)
        content = [
            text_block(f"Image 1 - PDF page {spec.page} with the crop outlined in red (crop box {box_px.to_list(0)} "
                       "px):"),
            image_block(overlay_box(page_img, box_px)),
            text_block("Image 2 - the cropped image:"),
            image_block(crop.image),
            text_block(prompts.FIGURE_TASK.format(
                page=spec.page, number=q.number, width=crop.image.width, height=crop.image.height, stem=q.stem,
                kind=spec.kind, alt=alt or "(none)",
                metrics=(f"Automatic crop measurements: {crop.metrics}. Adjustments made: "
                         f"{'; '.join(crop.notes) or 'none'}." + (f"\nReviewer comment: {context}" if context else "")))),
        ]
        problems_enum = ["wrong_figure", "cut_off", "too_much_whitespace", "includes_extra_text", "unreadable"]
        schema = {"type": "object", "properties": {
            "ok": {"type": "boolean"},
            "problems": {"type": "array", "items": {"type": "string", "enum": problems_enum}},
            "corrected_box": {"type": "object", "properties": {"page": {"type": "integer"}, **_BOX_PROPS,
                                                               "exact": {"type": "boolean"}},
                              "required": ["page", "x0", "y0", "x1", "y1", "exact"], "additionalProperties": False,
                              "description": "Better box in page-image pixels (repeat the current box if ok)."},
            "alt": {"type": "string"},
            "note": {"type": "string"}},
            "required": ["ok", "problems", "corrected_box", "alt", "note"], "additionalProperties": False}

        def validate(inp: dict) -> dict:
            if len(inp.get("alt", "").strip()) < 8:
                raise SubmitRejected("alt must describe the figure (at least a short sentence)")
            try:
                self._box_from_input(inp["corrected_box"])
            except (ValueError, KeyError, TypeError) as exc:
                raise SubmitRejected(f"corrected_box: {exc}") from exc
            return dict(inp)

        task = Task(name=f"figure-q{q.number}", system=prompts.SYSTEM_PROMPT, content=content,
                    tools=[self._view_region_tool(), self._preview_crop_tool()],
                    submit=Tool("submit_figure_check", "Submit the verdict on the crop.", schema, validate=validate),
                    max_turns=16)
        return self.runner.run(task)

    # -- verification (pass 2 and pass 3) ----------------------------------------
    def _render(self, qdict: dict) -> tuple[bytes | None, dict[str, str]]:
        if self._renderer is None:
            return None, {}
        try:
            r = self._renderer.render(qdict)
            return r.png, r.katex_errors
        except Exception as exc:  # noqa: BLE001
            log.warning("render failed for question %s: %s", qdict.get("number"), exc)
            return None, {}

    def _verify(self, q: QuestionState, qdict: dict, pass_name: str, render_png: bytes | None,
                issues: list[str]) -> dict:
        doc = self.doc
        content: list[dict] = []
        supplied: list[str] = []
        idx = 0

        def add(label: str, img) -> None:
            nonlocal idx
            idx += 1
            supplied.append(f"- Image {idx}: {label}")
            content.extend([text_block(f"Image {idx} - {label}:"), image_block(img)])

        for page, box in q.regions:
            add(f"zoomed crop of question {q.number} from PDF page {page} (the source)",
                doc.render_region_for_model(page, box))
        for page in sorted({p for p, _ in q.regions} | {q.source_page}):
            add(f"full PDF page {page} for context (page-image coordinates for view_region)", self._page_image(page))
        if render_png is not None:
            add("how the current transcription renders in the simulator (KaTeX)", render_png)
        for i, img in enumerate(qdict.get("images", [])):
            fig = q.figures[i] if i < len(q.figures) else None
            if fig is not None:
                add(f"attached figure {i + 1} (cropped from page {fig.spec.page}; alt: {img['alt']})", fig.crop.image)

        options_text = "\n".join(f"{o['label']}: {o['content']}" for o in qdict["options"])
        issue_text = ("\nAutomated checks reported these problems, which must be fixed:\n- " + "\n- ".join(issues)
                      + "\n") if issues else ""
        if q.review_reasons:
            issue_text += ("\nEarlier notes on this question (check whether they still apply):\n- "
                           + "\n- ".join(q.review_reasons) + "\n")
        figures_text = f"Attached figures: {len(qdict.get('images', []))}\n"
        render_hint = ("\nThe KaTeX render shows exactly what the student will see: compare it against the source "
                       "crop for grouping (fractions, roots, exponents, brackets).") if render_png is not None else ""
        content.append(text_block(prompts.VERIFY_TASK.format(
            pass_name=pass_name, number=q.number, supplied="\n".join(supplied), stem=qdict["stem"],
            options=options_text, figures=figures_text, issues=issue_text, render_hint=render_hint)))

        current_options = [dict(o) for o in qdict["options"]]
        attempts = {"n": 0}

        def validate(inp: dict) -> dict:
            attempts["n"] += 1
            problems = _content_problems("", inp.get("stem"), inp.get("options"))
            for j, f in enumerate(inp.get("missing_figures", [])):
                try:
                    self._box_from_input(f)
                except (ValueError, KeyError, TypeError) as exc:
                    problems.append(f"missing_figures[{j}]: {exc}")
            if problems and attempts["n"] < 3:
                raise SubmitRejected("\n".join(problems))
            out = dict(inp)
            out["_problems"] = problems
            changed = inp["stem"] != qdict["stem"] or [dict(o) for o in inp["options"]] != current_options
            out["changed"] = changed
            return out

        schema = {"type": "object", "properties": {
            "verdict": {"type": "string", "enum": ["match", "corrected"]},
            "discrepancies": {"type": "array", "items": {"type": "object", "properties": {
                "where": {"type": "string"}, "source_shows": {"type": "string"}, "transcription_had": {"type": "string"}},
                "required": ["where", "source_shows", "transcription_had"], "additionalProperties": False}},
            "stem": {"type": "string"},
            "options": {"type": "array", "items": OPTION_SCHEMA},
            "figures_ok": {"type": "boolean"},
            "figure_problems": {"type": "string"},
            "missing_figures": {"type": "array", "items": FIGURE_SCHEMA},
            "needs_review": {"type": "boolean"},
            "review_note": {"type": "string"}},
            "required": ["verdict", "discrepancies", "stem", "options", "figures_ok", "figure_problems",
                         "missing_figures", "needs_review", "review_note"], "additionalProperties": False}
        task = Task(name=f"{pass_name.lower().replace(' ', '')}-q{q.number}", system=prompts.SYSTEM_PROMPT,
                    content=content, tools=[self._view_region_tool()],
                    submit=Tool("submit_verification", "Submit the verification result for this question.", schema,
                                validate=validate),
                    max_turns=30)
        return self.runner.run(task)

    def _apply_verification(self, q: QuestionState, res: dict, label: str) -> bool:
        """Apply a verifier result; returns True if the question changed (needs another check)."""
        changed = False
        if res["changed"]:
            diffs = "; ".join(f"{d['where']}: '{d['transcription_had']}' -> '{d['source_shows']}'"
                              for d in res["discrepancies"]) or "text changed"
            q.history.append(f"{label}: corrected ({diffs})")
            q.stem = res["stem"]
            q.options = [dict(o) for o in res["options"]]
            changed = True
        elif res["verdict"] == "corrected":
            q.history.append(f"{label}: reported discrepancies but returned identical text")
        else:
            q.history.append(f"{label}: matches source")
        for p in res.get("_problems", []):
            q.flag(f"{label}: {p}")
        if res["needs_review"]:
            q.flag(f"{label}: {res['review_note'] or 'flagged by verifier'}")
        for f in res["missing_figures"]:
            try:
                page, box_px = self._box_from_input(f)
                spec = FigureSpec(page, self.doc.px_to_pt(page, box_px), f["kind"], f["alt"])
                fig = self._finalize_figure(q, spec)
            except (ValueError, KeyError, TaskFailed) as exc:
                q.flag(f"{label}: reported a missing figure that could not be cropped ({exc})")
                continue
            q.figure_specs.append(spec)
            q.figures.append(fig)
            q.history.append(f"{label}: added missing figure from page {page}")
            if not fig.confirmed:
                q.flag("image crop could not be confirmed (added during verification)")
            changed = True
        if not res["figures_ok"] and q.figures:
            q.history.append(f"{label}: figure problem reported - {res['figure_problems']}")
            try:
                rechecked = [self._finalize_figure(q, f.spec, context=res["figure_problems"]) for f in q.figures]
            except TaskFailed as exc:
                q.flag(f"figure problem could not be re-checked: {res['figure_problems']} ({exc})")
                return changed
            if [f.crop.box_pt for f in rechecked] != [f.crop.box_pt for f in q.figures]:
                changed = True
            q.figures = rechecked
            if not all(f.confirmed for f in rechecked):
                q.flag(f"figure problem: {res['figure_problems']}")
        return changed

    def _question_dict(self, q: QuestionState) -> dict:
        return q.to_question().model_dump(mode="json")

    def _verify_rounds(self, drafts: dict[int, QuestionState], pass_name: str, rounds: int,
                       final_round_flags: bool) -> None:
        pending = sorted(drafts)
        for rnd in range(1, rounds + 1):
            label = f"{pass_name} round {rnd}"
            self.progress("pass2", f"{label}: verifying {len(pending)} questions against the source")
            prepared = []
            for n in pending:  # rendering must stay on this thread (Playwright)
                qd = self._question_dict(drafts[n])
                png, kerr = self._render(qd)
                issues = [f"{fld}: KaTeX cannot render it ({msg})" for fld, msg in kerr.items()]
                issues += _string_issues(qd)
                prepared.append((n, qd, png, issues))
            results = self._map(lambda item: (item[0], self._verify(drafts[item[0]], item[1], label, item[2], item[3])),
                                prepared, "pass2", "question")
            done = {n for n, _ in results}
            for n in pending:
                if n not in done:
                    drafts[n].flag(f"{label}: verification failed to run")
            next_pending = []
            for n, res in sorted(results, key=lambda r: r[0]):
                if self._apply_verification(drafts[n], res, label):
                    next_pending.append(n)
            pending = next_pending
            if not pending:
                return
        if final_round_flags:
            for n in pending:
                drafts[n].flag(f"{pass_name}: still being corrected after {rounds} verification rounds - check "
                               "the latest corrections by hand")

    def _pass3(self, drafts: dict[int, QuestionState], data: dict, report: ValidationReport, targets: list[int],
               rnd: int) -> list[int]:
        """Verify questions as parsed from the written file; returns numbers that changed."""
        by_num = {q["number"]: q for q in data["questions"]}
        prepared = []
        for n in targets:
            qd = by_num.get(n)
            if qd is None:
                continue
            png, kerr = self._render(qd)
            issues = [f"{i.field or 'question'}: {i.message}" for i in report.for_question(n) if i.level == "error"]
            issues += [f"{fld}: KaTeX cannot render it ({msg})" for fld, msg in kerr.items()]
            prepared.append((n, qd, png, issues))
        label = f"Pass 3 round {rnd} (final file)"
        results = self._map(lambda item: (item[0], self._verify(drafts[item[0]], item[1], label, item[2], item[3])),
                            prepared, "pass3", "question")
        changed = []
        for n, res in sorted(results, key=lambda r: r[0]):
            if self._apply_verification(drafts[n], res, label):
                changed.append(n)
        return changed

    def _validate(self, data: dict, expected: int | None, meta: dict) -> ValidationReport:
        katex_errors: dict[tuple[int, str], str] = {}
        if self._renderer is not None:
            for q in data.get("questions", []):
                _, kerr = self._render(q)
                for fld, msg in kerr.items():
                    katex_errors[(q["number"], fld)] = msg
        expected_duration = self.opt.duration_minutes or int(meta.get("duration_minutes") or 0) or None
        return validate_paper(data, expected_questions=expected, expected_duration=expected_duration, pdf=self.doc,
                              katex_errors=katex_errors)


# ----------------------------------------------------------------------------- module helpers
def _content_problems(where: str, stem: Any, options: Any) -> list[str]:
    """Error-level string problems in a submitted stem/options (fed back to Claude)."""
    prefix = f"{where}: " if where else ""
    problems = []
    if not isinstance(options, list) or len(options) < 2:
        problems.append(f"{prefix}needs at least two options")
        options = options if isinstance(options, list) else []
    for i, o in enumerate(options):
        want = chr(ord("A") + i)
        if o.get("label") != want:
            problems.append(f"{prefix}option {i + 1} label must be '{want}' (labels are sequential A, B, C, ...)")
    r = ValidationReport()
    check_string(r, stem, None, "stem")
    for o in options:
        check_string(r, o.get("content"), None, f"option {o.get('label')}")
    problems += [f"{prefix}{i.field}: {i.message}" for i in r.errors]
    return problems


def _string_issues(qd: dict) -> list[str]:
    r = ValidationReport()
    check_string(r, qd["stem"], qd["number"], "stem")
    for o in qd["options"]:
        check_string(r, o["content"], qd["number"], f"option {o['label']}")
    return [f"{i.field}: {i.message}" for i in r.errors]


def _leading_number(problem: str) -> int | None:
    m = re.match(r"question (\d+)", problem)
    return int(m.group(1)) if m else None


def _same_box(a: Box, b: Box, tol: float = 2.0) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a.to_list(3), b.to_list(3)))


def convert_pdf(pdf_path: str | Path, runner: ClaudeRunner, options: ConvertOptions | None = None,
                out_dir: str | Path | None = None, progress: Progress | None = None,
                renderer: QuestionRenderer | None = None) -> ConversionResult:
    return Converter(runner, options, progress, renderer).convert(pdf_path, out_dir)


