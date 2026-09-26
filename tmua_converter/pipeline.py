"""Deterministic PDF -> .tmua.json conversion (no AI, no network).

For each question the converter:

1. finds the question's area of the page from its printed question number,
2. crops every diagram / graph / table from the rendered page,
3. rebuilds the stem and options (text + LaTeX) from the characters and
   lines the PDF stores (see :mod:`tmua_converter.mathlayout`),
4. cross-checks the result against the PDF's own text layer: every letter and
   digit on the page must appear in the output, and nothing extra,
5. writes the file, reloads it with a JSON parser and validates it (plus a
   KaTeX render of every expression when a headless browser is available).

Anything uncertain is flagged ``needsReview`` with the reason.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import mathlayout as ml
from .figures import CropResult, crop_figure
from .naming import paper_filename, paper_id
from .ocr import images_to_pdf, is_image_file
from .pdf import Box, PdfDocument
from .render import QuestionRenderer, try_start_renderer
from .schema import Image, Option, Paper, Question, load_paper_dict, write_paper
from .validator import ValidationReport, validate_paper

log = logging.getLogger(__name__)

Progress = Callable[[str, str], None]

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
NUMBER_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "twenty-five": 25, "thirty-five": 35})
LABELS = "ABCDEFGH"


class ConversionError(RuntimeError):
    pass


@dataclass
class ConvertOptions:
    render_check: bool = True
    title: str | None = None
    paper: str | None = None
    year: str | None = None
    duration_minutes: int | None = None
    expected_questions: int | None = None
    filename: str | None = None
    avoid_names: frozenset = frozenset()  # file names already produced in this batch


@dataclass
class FinalFigure:
    crop: CropResult
    alt: str
    labels: list[str] = field(default_factory=list)


@dataclass
class QuestionState:
    number: int
    stem: str
    options: list[dict]
    source_page: int
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
        return Question(number=self.number, stem=self.stem,
                        options=[Option(label=o["label"], content=o["content"]) for o in self.options],
                        images=[Image(src=f.crop.data_uri, alt=f.alt) for f in self.figures],
                        sourcePage=self.source_page, needsReview=self.needs_review)


@dataclass
class ConversionResult:
    paper: Paper
    output_path: Path | None
    report: ValidationReport
    metadata: dict
    questions: dict[int, QuestionState]
    render_check_used: bool
    notes: list[str]
    source_pdf: Path | None = None

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
            "notes": self.notes,
        }


# ----------------------------------------------------------------------------- metadata
def _words_to_int(w: str) -> int | None:
    w = w.lower()
    if w.isdigit():
        return int(w)
    return NUMBER_WORDS.get(w)


def parse_duration(text: str) -> tuple[int | None, str]:
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"\b(\d+|one|two|three)\s*(?:hours?|hrs?)\s*(?:and\s*)?(\d+)\s*(?:minutes|mins?)\b", t, re.I)
    if m:
        return _words_to_int(m.group(1)) * 60 + int(m.group(2)), m.group(0)
    m = re.search(r"\b(\d+)\s*(?:minutes|mins?)\b", t, re.I)
    if m:
        return int(m.group(1)), m.group(0)
    m = re.search(r"\b(\d+(?:\.\d+)?|one|two|three)\s*(?:hours?|hrs?)\b", t, re.I)
    if m:
        v = m.group(1)
        hours = float(v) if v[0].isdigit() else _words_to_int(v)
        return int(round(hours * 60)), m.group(0)
    return None, ""


def parse_question_count(text: str) -> tuple[int | None, str]:
    t = re.sub(r"\s+", " ", text)
    for m in re.finditer(r"\b(\d{1,2}|[a-z]+(?:-[a-z]+)?)\s+(?:multiple[- ]choice\s+)?questions\b", t, re.I):
        n = _words_to_int(m.group(1))
        if n:
            return n, m.group(0)
    return None, ""


def read_metadata(doc: PdfDocument, filename: str) -> dict:
    first = "\n".join(doc.text(p) for p in range(1, min(doc.page_count, 2) + 1))
    lines = [ln for ln in doc.content_lines(1) if len(ln.text) > 1]
    title = ""
    if lines:
        biggest = max(ln.size for ln in lines)
        top = [ln.text.strip() for ln in lines if ln.size >= 0.8 * biggest][:3]
        title = re.sub(r"\s+", " ", " ".join(top)).strip()
    stem = re.sub(r"^\d{2}_", "", Path(filename).stem.replace(".source", ""))  # web uploads are numbered 01_, 02_
    stem = stem.replace("_", " ").replace("-", " ")
    ocr_cover = doc.is_ocr(1)
    if not title or len(title) > 120 or (ocr_cover and len(re.findall(r"[A-Za-z]{3,}", title)) < 2):
        title = stem
    paper_m = re.search(r"\bPaper\s*(\d+|[IVX]+)\b", first + " " + stem, re.I)
    year_m = re.search(r"\b(19[89]\d|20\d\d)\b", first) or re.search(r"\b(19[89]\d|20\d\d)\b", stem)
    set_m = re.search(r"\b((?:Set|Mock|Practice)\s*\d+|Specimen)\b", first + " " + stem, re.I)
    duration, dur_ev = parse_duration(first)
    count, count_ev = parse_question_count(first)
    return {
        "title": title,
        "paper": f"Paper {paper_m.group(1)}" if paper_m else "",
        "year": year_m.group(1) if year_m else (set_m.group(1) if set_m else ""),
        "duration_minutes": duration or 0,
        "duration_evidence": dur_ev,
        "stated_question_count": count or 0,
        "count_evidence": count_ev,
    }


# ----------------------------------------------------------------------------- helpers
def _alnum(text: str) -> Counter:
    """Letters and digits a reader would see, from LaTeX-in-text or plain text."""
    t = text
    for sym, cmd in ml.GREEK.items():
        t = re.sub(re.escape(cmd) + r"(?![A-Za-z])", sym, t)
    t = re.sub(r"\\mathbb\{[A-Z]\}", " ", t)
    t = re.sub(r"\\(?!(?:" + "|".join(ml.FUNCTIONS) + r")(?![A-Za-z]))[A-Za-z]+", " ", t)  # drop other commands
    t = re.sub(r"\\([A-Za-z]+)", r"\1", t)  # \log -> log
    t = unicodedata.normalize("NFKC", t)
    return Counter(c for c in t if c.isalnum())


def _xextent(items: list[ml.Item]) -> tuple[float, float]:
    return min(i.x0 for i in items), max(i.x1 for i in items)


def _inside(g: ml.Item, box: Box) -> bool:
    return box.x0 <= g.cx <= box.x1 and box.y0 <= g.cy <= box.y1


# ----------------------------------------------------------------------------- converter
class Converter:
    def __init__(self, options: ConvertOptions | None = None, progress: Progress | None = None,
                 renderer: QuestionRenderer | None = None):
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
        if is_image_file(pdf_path):  # a photo/screenshot: make it a one-page PDF first
            target = Path(out_dir) if out_dir else pdf_path.parent
            target.mkdir(parents=True, exist_ok=True)
            pdf_path = images_to_pdf([pdf_path], target / (pdf_path.stem + ".source.pdf"))
        self.source_pdf = pdf_path
        self.doc = PdfDocument(pdf_path)
        self.notes: list[str] = []
        self._glyph_cache: dict[int, list[ml.Glyph]] = {}
        self._ignored: set[int] = set()
        self._ocr_rules: dict[int, list[ml.Rule]] = {}
        try:
            if self._renderer is None and self.opt.render_check:
                self._renderer = try_start_renderer()
                self._own_renderer = self._renderer is not None
                if self._renderer is None:
                    self.notes.append("KaTeX render check skipped (install playwright + Chromium to enable it).")
            return self._convert(pdf_path, Path(out_dir) if out_dir else pdf_path.parent)
        finally:
            if self._own_renderer and self._renderer is not None:
                self._renderer.close()
                self._renderer = None
            self.doc.close()

    def _convert(self, pdf_path: Path, out_dir: Path) -> ConversionResult:
        doc = self.doc
        if not doc.has_text_layer():
            from .ocr import INSTALL_HELP

            if doc.ocr_missing_pages:
                raise ConversionError(f"{pdf_path.name} is a picture (scan/photo), so its text has to be read with "
                                      f"OCR.\n{INSTALL_HELP}")
            raise ConversionError(f"No text could be read from {pdf_path.name}.")
        if doc.ocr_missing_pages:
            self.notes.append("Some pages contain pictures of text that were not read because Tesseract OCR is not "
                              "installed; those pictures were kept as images.")
        if doc.ocr_areas:
            self.notes.append("Text on page(s) " + ", ".join(map(str, sorted(doc.ocr_areas))) + " was read from "
                              "pictures with OCR; those questions are marked for review.")
        self.progress("metadata", f"Reading the cover of {pdf_path.name} ({doc.page_count} pages)")
        meta = read_metadata(doc, pdf_path.name)
        anchors = doc.find_question_anchors()
        if not anchors:
            raise ConversionError("No question numbers were found in the left margin, so the questions could not "
                                  "be located.")
        expected = self.opt.expected_questions or meta.get("stated_question_count") or None
        self.progress("pass1", f"Found {len(anchors)} questions; rebuilding text and maths from the page layout")

        questions: dict[int, QuestionState] = {}
        for n in sorted(anchors):
            q = questions[n] = self._question(n)
            self.progress("pass1", f"Question {n}: {len(q.options)} options, {len(q.figures)} image(s)"
                          + (" - flagged for review" if q.needs_review else ""))
        if expected and expected != len(anchors):
            self.notes.append(f"The paper says it has {expected} questions but {len(anchors)} question numbers "
                              "were found.")

        # ------------------------------------------------------------ pass 2: cross-check vs the PDF text layer
        self.progress("pass2", "Cross-checking every question against the characters printed in the PDF")
        for q in questions.values():
            self._cross_check(q)

        # ------------------------------------------------------------ pass 3: write, reload, validate
        title, paper_name, year, duration = self._final_metadata(meta)
        name = self.opt.filename or paper_filename(title, paper_name)
        k = 2
        base = name[: -len(".tmua.json")] if name.endswith(".tmua.json") else name
        while name in self.opt.avoid_names:
            name = f"{base}_{k}.tmua.json"
            k += 1
        out_path = out_dir / name

        def build() -> Paper:
            return Paper(id=paper_id(title, paper_name), title=title, paper=paper_name, year=year,
                         durationMinutes=duration, questions=[questions[n].to_question() for n in sorted(questions)])

        write_paper(build(), out_path)
        self.progress("pass3", f"Wrote {out_path.name}; reloading it with a JSON parser and validating")
        report = self._validate(load_paper_dict(out_path), expected, meta)
        for issue in report.errors:
            if issue.question in questions:
                questions[issue.question].flag(f"validator: {issue.message}")
        write_paper(build(), out_path)
        data = load_paper_dict(out_path)
        report = self._validate(data, expected, meta)
        paper = Paper.model_validate(data)
        flagged = sum(q.needsReview for q in paper.questions)
        self.progress("done", f"Finished: {len(paper.questions)} questions, {len(report.errors)} validation errors, "
                              f"{flagged} flagged for review")
        return ConversionResult(paper=paper, output_path=out_path, report=report, metadata=meta, questions=questions,
                                render_check_used=self._renderer is not None, notes=self.notes,
                                source_pdf=self.source_pdf)

    def _final_metadata(self, meta: dict) -> tuple[str, str, str, int]:
        title = self.opt.title or meta["title"]
        paper_name = self.opt.paper if self.opt.paper is not None else meta.get("paper", "")
        year = self.opt.year or meta.get("year") or "Unknown"
        duration = self.opt.duration_minutes or int(meta.get("duration_minutes") or 0)
        if not duration:
            duration = 75
            self.notes.append("The PDF does not state a time allowed, so durationMinutes was set to 75 (the TMUA "
                              "standard). Change it with --duration or on the review page if that is wrong.")
        if not paper_name:
            paper_name = "Paper 1"
            self.notes.append("The PDF does not name the paper, so the 'paper' field was set to 'Paper 1'.")
        return title, paper_name, year, duration

    # ======================================================================= one question
    def _glyphs(self, page: int) -> list[ml.Glyph]:
        if page not in self._glyph_cache:
            raw = self.doc.rawdict(page)
            with self.doc._lock:
                glyphs = ml.page_glyphs(self.doc._page(page), page, raw=raw)
            furniture = [ln.box for ln in self.doc.lines(page) if self.doc.is_furniture(ln, page)]
            glyphs = [g for g in glyphs if not any(_inside(g, f.expand(1)) for f in furniture)]
            dash_rules, glyphs = ml.ocr_dash_rules(glyphs)
            self._ocr_rules[page] = dash_rules
            self._glyph_cache[page] = glyphs
        return self._glyph_cache[page]

    def _rules(self, page: int) -> list[ml.Rule]:
        self._glyphs(page)
        with self.doc._lock:
            return ml.page_rules(self.doc._page(page)) + self._ocr_rules.get(page, [])

    def _is_anchor_glyph(self, g: ml.Glyph, n: int) -> bool:
        a = self.doc.find_question_anchors()[n]
        return g.page == a.page and a.box.expand(0.5).contains(Box(g.x0, g.y0, g.x1, g.y1), tol=0.5)

    def _question(self, n: int) -> QuestionState:
        doc = self.doc
        anchor = doc.find_question_anchors()[n]
        regions = doc.question_regions(n)
        q = QuestionState(number=n, stem="", options=[], source_page=anchor.page, regions=regions)
        problems: list[str] = []
        glyphs: list[ml.Glyph] = []
        rules: list[ml.Rule] = []
        for page, box in regions:
            page_g = [g for g in self._glyphs(page) if _inside(g, box) and not self._is_anchor_glyph(g, n)]
            page_r = [r for r in self._rules(page) if box.x0 - 1 <= r.x0 and r.x1 <= box.x1 + 1
                      and box.y0 <= r.y <= box.y1]
            S = ml.body_size(page_g) if page_g else 10.0
            for crop in self._figures(page, box, S):
                inside = [g for g in page_g if _inside(g, crop.box_pt)]
                labels = [g.ch for g in sorted(inside, key=lambda g: (round(g.base / 4), g.x0))
                          if g.ch in LABELS and (g.bold or g.ocr) and not g.math_font]
                ids = {id(g) for g in inside}
                page_g = [g for g in page_g if id(g) not in ids]
                page_r = [r for r in page_r if not (crop.box_pt.x0 - 1 <= r.x0 and r.x1 <= crop.box_pt.x1 + 1
                                                    and crop.box_pt.y0 - 1 <= r.y <= crop.box_pt.y1 + 1)]
                q.figures.append(FinalFigure(crop, "", labels))
                if crop.metrics.get("edges_cutting_ink"):
                    q.flag(f"figure on page {page} may be cut off ({', '.join(crop.metrics['edges_cutting_ink'])})")
            glyphs += page_g
            rules += page_r

        if not glyphs:
            q.flag("no text found for this question")
            return q
        if any(g.ocr for g in glyphs):
            q.flag("read from a picture with OCR: free OCR is unreliable for maths symbols, so check the maths "
                   "against the picture")
        S = ml.body_size(glyphs)
        items = ml.Builder(glyphs, rules, problems).build()
        page_of = {}
        for it in items:
            gs = it.glyphs()
            page_of[id(it)] = gs[0].page if gs else anchor.page
        lines = ml.build_lines(items, page_of, S)
        col = (min(ln.x0 for ln in lines), max(ln.x1 for ln in lines))

        # ---------------------------------------------------------------- option labels A, B, C, ...
        segments: dict[str | None, list[ml.Line]] = {None: []}
        found: list[str] = []
        current: str | None = None
        label_col: float | None = None  # x of the "A" label; OCR may glue later labels to their text
        for ln in lines:
            its = sorted(ln.items, key=lambda i: i.x0)
            cur: list[ml.Item] = []
            for k, it in enumerate(its):
                want = LABELS[len(found)] if len(found) < len(LABELS) else None
                nxt = its[k + 1] if k + 1 < len(its) else None
                prv = its[k - 1] if k else None
                spaced = (nxt is None or nxt.x0 - it.x1 >= 0.45 * S) and (prv is None or it.x0 - prv.x1 >= 1.0 * S)
                ocr_column = isinstance(it, ml.Glyph) and it.ocr and label_col is not None and k == 0 \
                    and abs(it.x0 - label_col) < 0.3 * S
                if want and isinstance(it, ml.Glyph) and it.ch == want and not it.math_font and (spaced or ocr_column):
                    if want == "A":
                        label_col = it.x0
                    if cur:
                        segments[current].append(_subline(cur, ln))
                    cur = []
                    current = want
                    found.append(want)
                    segments[current] = []
                    continue
                cur.append(it)
            if cur:
                segments[current].append(_subline(cur, ln))

        if any(g.ocr for g in glyphs):
            rows = self._ocr_label_rows(regions, lines, S, label_col)
            if len(rows) > len(found):
                found, segments = self._segments_from_rows(rows, lines, S)
                q.history.append(f"answer labels located from the picture ({len(rows)} rows)")

        # the last option ends at a big vertical gap (e.g. "END OF TEST" below it)
        if found:
            kept: list[ml.Line] = []
            lines_last = segments[found[-1]]
            for k, ln in enumerate(lines_last):
                if kept and (ln.page != kept[-1].page or ln.y0 - kept[-1].y1 > 2.5 * S):
                    dropped = lines_last[k:]
                    self._ignored.update(id(g) for d in dropped for it in d.items for g in it.glyphs())
                    q.history.append("ignored text printed below the last option: "
                                     + " / ".join(ml.lines_to_text([d], []) for d in dropped)[:80])
                    break
                kept.append(ln)
            segments[found[-1]] = kept

        q.stem = ml.lines_to_text(segments[None], problems, column=col)
        opts = [{"label": lab, "content": _numeric_as_maths(ml.lines_to_text(segments[lab], problems))}
                for lab in found]
        if len(opts) < 2:
            seq: list[str] = []
            fig_labels = [lab for f in q.figures for lab in f.labels]
            for lab in fig_labels:
                if len(seq) < len(LABELS) and lab == LABELS[len(seq)]:
                    seq.append(lab)
            if any(g.ocr for g in glyphs) and "A" in fig_labels and len(set(fig_labels)) >= 2:
                # OCR can miss a letter or two in a graph panel; the panel runs A, B, C, ... in order
                seq = list(LABELS[:LABELS.index(max(set(fig_labels) & set(LABELS))) + 1])
            if len(seq) >= 2:
                opts = [{"label": lab, "content": f"Graph {lab}"} for lab in seq]
                q.history.append("the options are the labelled graphs in the figure")
            else:
                q.flag("could not find the answer options A, B, C, ... - add them by hand")
        q.options = opts

        for f in q.figures:
            f.alt = _alt_text(q, f)
        for p in dict.fromkeys(problems):
            q.flag(p)
        if not q.stem:
            q.flag("the question text came out empty")
        for o in q.options:
            if not o["content"].strip():
                q.flag(f"option {o['label']} came out empty")
        pages = ", ".join(str(p) for p, _ in regions)
        q.history.append(f"pass 1: rebuilt from {len(glyphs)} characters and {sum(r.used for r in rules)} "
                         f"fraction/root bars on page(s) {pages}")
        return q

    def _ocr_label_rows(self, regions, lines, S: float, label_col: float | None) -> list[tuple[int, Box]]:
        """Answer labels in a picture, found from the ink: a letter-sized mark in the label column
        with clear space after it, below the question text.  Works even when OCR misreads the letter."""
        if label_col is None:
            cands = [it for ln in lines for it in ln.items
                     if isinstance(it, ml.Glyph) and it.ocr and it.ch in LABELS]
            xs = sorted(round(g.x0) for g in cands)
            best = max(xs, key=lambda x: sum(abs(x - o) < 3 for o in xs), default=None)
            if best is None or sum(abs(best - o) < 3 for o in xs) < 2:
                return []
            label_col = best
        rows: list[tuple[int, Box]] = []
        for page, box in regions:
            strip = Box(label_col - 0.4 * S, box.y0, label_col + 1.6 * S, box.y1).clamp(box)
            wide = Box(label_col - 0.4 * S, box.y0, min(box.x1, label_col + 3.0 * S), box.y1).clamp(box)
            blobs = self.doc.ink_components(page, wide, [])
            for b in blobs:
                if not (abs(b.x0 - label_col) < 0.35 * S and 0.4 * S <= b.height <= 1.4 * S
                        and 0.3 * S <= b.width <= 1.3 * S and strip.contains(b, tol=1)):
                    continue
                gap_ok = not any(o is not b and o.x0 < b.x1 + 0.45 * S and o.x1 > b.x1 and
                                 min(o.y1, b.y1) - max(o.y0, b.y0) > 0.3 * b.height for o in blobs)
                if gap_ok:
                    rows.append((page, b))
        rows.sort(key=lambda r: (r[0], r[1].y0))
        return rows[:len(LABELS)]

    def _segments_from_rows(self, rows, lines, S):
        """Split lines into stem and options by the vertical position of the label rows."""
        found = [LABELS[i] for i in range(len(rows))]
        segments: dict[str | None, list[ml.Line]] = {None: []}
        for lab in found:
            segments[lab] = []

        def owner(page: int, y: float) -> str | None:
            label = None
            for i, (p, b) in enumerate(rows):
                if (p, b.y0 - 0.35 * S) <= (page, y):
                    label = found[i]
            return label

        for ln in lines:
            keep = [it for it in ln.items
                    if not any(p == ln.page and b.expand(1.0).x0 <= it.cx <= b.expand(1.0).x1
                               and b.expand(1.0).y0 <= it.cy <= b.expand(1.0).y1 for p, b in rows)]
            by_label: dict[str | None, list[ml.Item]] = {}
            for it in keep:
                by_label.setdefault(owner(ln.page, it.cy), []).append(it)
            for lab, its in by_label.items():
                segments[lab].append(_subline(its, ln))
        return found, segments

    def _figures(self, page: int, region: Box, S: float) -> list[CropResult]:
        """Crops for diagrams/graphs/tables inside a question's region."""
        ocr = self.doc.is_ocr(page, region)
        min_size, join_gap = (3.0 * S, 4.0 * S) if ocr else (1.6 * S, 2.5 * S)
        boxes = [g.clamp(region) for g in self.doc.graphic_boxes(page)
                 if region.overlap_fraction(g) > 0.5 and g.width >= min_size and g.height >= min_size]
        merged = [b for b in boxes if not b.is_empty()]
        changed = True
        while changed:
            changed = False
            for i in range(len(merged)):
                for j in range(i + 1, len(merged)):
                    if merged[i].gap_to(merged[j]) < join_gap:
                        merged[i] = merged[i].union(merged.pop(j))
                        changed = True
                        break
                if changed:
                    break
        merged.sort(key=lambda b: (b.y0, b.x0))
        return [crop_figure(self.doc, page, b.expand(1.0)) for b in merged]

    # ======================================================================= checks
    def _cross_check(self, q: QuestionState) -> None:
        """Every letter/digit printed in the question must be in the output, and nothing extra."""
        expected: Counter = Counter()
        for page, box in q.regions:
            for g in self._glyphs(page):
                if not _inside(g, box) or self._is_anchor_glyph(g, q.number) or id(g) in self._ignored:
                    continue
                if any(f.crop.page == page and _inside(g, f.crop.box_pt) for f in q.figures):
                    continue
                expected.update(c for c in unicodedata.normalize("NFKC", g.ch) if c.isalnum())
        got = _alnum(q.stem)
        graph_options = bool(q.options) and all(o["content"] == f"Graph {o['label']}" for o in q.options)
        if not graph_options:
            for o in q.options:
                got += _alnum(o["content"])
                got[o["label"]] += 1  # the printed label letter itself
        missing, extra = expected - got, got - expected
        if missing or extra:
            detail = []
            if missing:
                detail.append("missing " + " ".join(sorted(missing.elements())))
            if extra:
                detail.append("extra " + " ".join(sorted(extra.elements())))
            q.flag("cross-check against the PDF's characters failed (" + "; ".join(detail) + ")")
        else:
            q.history.append("pass 2: every letter and digit printed in the question is in the output exactly once")

    def _validate(self, data: dict, expected: int | None, meta: dict) -> ValidationReport:
        katex_errors: dict[tuple[int, str], str] = {}
        if self._renderer is not None:
            for qd in data.get("questions", []):
                try:
                    res = self._renderer.render(qd)
                except Exception as exc:  # noqa: BLE001
                    log.warning("render failed: %s", exc)
                    continue
                for fld, msg in res.katex_errors.items():
                    katex_errors[(qd["number"], fld)] = msg
        expected_duration = self.opt.duration_minutes or int(meta.get("duration_minutes") or 0) or None
        return validate_paper(data, expected_questions=expected, expected_duration=expected_duration, pdf=self.doc,
                              katex_errors=katex_errors)


def _numeric_as_maths(text: str) -> str:
    """An answer that is just a number is written as maths, like every other expression."""
    m = re.fullmatch(r"([-−]?)\s*(\d+(?:\.\d+)?)", text.strip())
    return f"${'-' if m.group(1) else ''}{m.group(2)}$" if m else text


def _subline(items: list[ml.Item], ln: ml.Line) -> ml.Line:
    x0, x1 = _xextent(items)
    return ml.Line(items, ln.base, x0, x1, min(i.y0 for i in items), max(i.y1 for i in items), ln.page)


def _alt_text(q: QuestionState, f: FinalFigure) -> str:
    if len(f.labels) >= 2 and q.options and all(o["content"].startswith("Graph ") for o in q.options):
        return f"Graphs labelled {', '.join(f.labels)} for question {q.number} (the answer options)"
    return f"Diagram for question {q.number}, cropped from page {f.crop.page} of the paper"


def convert_pdf(pdf_path: str | Path, options: ConvertOptions | None = None, out_dir: str | Path | None = None,
                progress: Progress | None = None, renderer: QuestionRenderer | None = None) -> ConversionResult:
    return Converter(options, progress, renderer).convert(pdf_path, out_dir)
