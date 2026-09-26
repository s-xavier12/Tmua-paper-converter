"""Deterministic validation of a ``.tmua.json`` paper.

Run on the *reloaded* file (after ``json.load``), so the checks see exactly
what the simulator will see.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from PIL import Image as PILImage

from . import latex
from .figures import OUTPUT_ZOOM, decode_data_uri

TOP_LEVEL_KEYS = ["formatVersion", "id", "title", "paper", "year", "durationMinutes", "questions"]
QUESTION_KEYS = ["number", "stem", "options", "images", "sourcePage", "needsReview"]
FORBIDDEN_KEYS = {
    "correctanswer", "correct_answer", "answer", "answers", "answerkey", "answer_key", "correct", "solution",
    "solutions", "explanation", "explanations", "marks", "mark", "reasoning", "workedsolution", "hint", "hints",
}

# Words that are never English, so seeing them bare means a lost backslash.
BARE_ERROR_WORDS = ("frac dfrac tfrac sqrt binom dbinom infty times cdot leq geq neq mathrm lfloor rfloor lceil "
                    "rceil ldots cdots rightarrow Rightarrow approx equiv alpha beta gamma delta theta lambda sigma "
                    "omega varepsilon epsilon circ pm").split()
# Bare in maths these render as italic letters - almost always a lost backslash.
BARE_WARNING_WORDS = "sin cos tan sec cosec csc cot log ln exp lim sum int prod pi left right le ge ne max min".split()
_BARE_ERR_RE = re.compile(r"(?<![A-Za-z\\])(" + "|".join(BARE_ERROR_WORDS) + r")(?![A-Za-z])")
_BARE_WARN_RE = re.compile(r"(?<![A-Za-z\\])(" + "|".join(BARE_WARNING_WORDS) + r")(?![A-Za-z])")
_TEXT_BARE_RE = re.compile(r"(?<![A-Za-z\\])(frac|dfrac|sqrt|binom|infty|leq|geq|neq|mathrm)(?![A-Za-z])")
# A real newline eating the "\n" of a command: "\neq" parsed as newline + "eq".
_EATEN_N_RE = re.compile(r"\n(eq|e|u|abla|ot|otin|eg|mid|i|leq|geq|subseteq|parallel|less|gtr)(?![A-Za-z])")
_MOJIBAKE_RE = re.compile(r"(Ã.|â€|Â[^A-Za-z0-9\s])")
_FIGURE_WORDS_RE = re.compile(r"\b(diagram|figure|graph|graphs|shown|sketch|table below|following table|shaded)\b",
                              re.I)


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    code: str
    message: str
    question: int | None = None
    field: str | None = None


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: str, code: str, message: str, question: int | None = None, field: str | None = None):
        self.issues.append(Issue(level, code, message, question, field))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def for_question(self, number: int) -> list[Issue]:
        return [i for i in self.issues if i.question == number]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": len(self.errors), "warnings": len(self.warnings),
                "issues": [asdict(i) for i in self.issues]}

    def format(self) -> str:
        if not self.issues:
            return "All checks passed."
        lines = []
        for i in self.issues:
            where = f"Q{i.question}" if i.question is not None else "paper"
            if i.field:
                where += f" {i.field}"
            lines.append(f"[{i.level.upper()}] {where}: {i.message} ({i.code})")
        return "\n".join(lines)


# --------------------------------------------------------------------------- strings
def check_string(report: ValidationReport, s: Any, q: int | None, fld: str, allow_empty: bool = False) -> None:
    """All per-string checks: characters, delimiters, braces, backslashes."""
    if not isinstance(s, str):
        report.add("error", "type", f"expected a string, got {type(s).__name__}", q, fld)
        return
    if not s.strip():
        if not allow_empty:
            report.add("error", "empty", "empty string", q, fld)
        return

    # --- characters -----------------------------------------------------------
    for idx, ch in enumerate(s):
        cat = unicodedata.category(ch)
        if ch == "\n":
            continue
        if ch == "\ufffd":
            report.add("error", "replacement-char", f"U+FFFD replacement character at offset {idx} (corrupted text)",
                       q, fld)
        elif cat == "Cc":
            hint = {"\t": " - an unescaped \\t (e.g. \\times/\\theta/\\tan) was parsed as a TAB",
                    "\f": " - an unescaped \\f (e.g. \\frac) was parsed as a form feed",
                    "\b": " - an unescaped \\b (e.g. \\beta/\\binom) was parsed as a backspace",
                    "\r": " - an unescaped \\r (e.g. \\right/\\rho) was parsed as a carriage return"}.get(ch, "")
            report.add("error", "control-char", f"control character U+{ord(ch):04X} at offset {idx}{hint}", q, fld)
        elif cat in ("Co", "Cs"):
            report.add("error", "private-char", f"private-use/surrogate character U+{ord(ch):04X} at offset {idx} "
                       "(usually a garbled maths glyph from the PDF text layer)", q, fld)
        elif cat == "Cf":
            report.add("error", "invisible-char", f"invisible format character U+{ord(ch):04X} at offset {idx}", q, fld)
    if _MOJIBAKE_RE.search(s):
        report.add("warning", "mojibake", "text looks mis-decoded (mojibake such as 'Ã' or 'â€')", q, fld)

    # --- literal backslash-n / doubled backslashes / unknown commands ----------
    for cmd in latex.commands(s):
        if cmd.backslashes >= 2:
            if cmd.name[:1].isalpha():
                shown = "\\" * cmd.backslashes + cmd.name
                report.add("error", "double-backslash",
                           f"doubled backslash before '{cmd.name}' ('{shown}') - after parsing there must be "
                           "exactly one backslash", q, fld)
            else:
                report.add("warning", "latex-linebreak",
                           "LaTeX line break '\\\\' found - prefer separate display-maths lines", q, fld)
            continue
        name = cmd.name
        if name == "n" or (name.startswith("n") and name.isalpha() and name not in latex.N_COMMANDS):
            report.add("error", "literal-backslash-n",
                       f"literal backslash-n ('\\{name[:12]}') - paragraph breaks must be real newline characters",
                       q, fld)
        elif name in ("t", "r") or (name.isalpha() and name not in latex.KNOWN_COMMANDS):
            report.add("warning", "unknown-command", f"unrecognised LaTeX command '\\{name}'", q, fld)

    if _EATEN_N_RE.search(s):
        m = _EATEN_N_RE.search(s)
        seg_kind = _segment_kind_at(s, m.start())
        if seg_kind != "text":
            report.add("error", "eaten-backslash-n",
                       f"newline followed by '{m.group(1)}' inside maths - looks like '\\n{m.group(1)}' lost its "
                       "backslash-n to a newline", q, fld)

    # --- $ delimiters ---------------------------------------------------------
    segs, problems = latex.split_math(s)
    for p in problems:
        report.add("error", "dollar-balance", f"{p.message} (offset {p.offset})", q, fld)

    # --- per-segment checks ---------------------------------------------------
    for seg in segs:
        if seg.kind == "text":
            for cmd in latex.commands(seg.content):
                if cmd.name[:1].isalpha() and not (cmd.name.startswith("n") and cmd.name not in latex.N_COMMANDS):
                    report.add("error", "command-outside-math",
                               f"LaTeX command '\\{cmd.name}' outside $...$ will display as raw text", q, fld)
            m = _TEXT_BARE_RE.search(seg.content)
            if m:
                report.add("error", "bare-command", f"bare '{m.group(1)}' in text - missing backslash/maths?", q, fld)
            ok, why = latex.brace_balance(seg.content)
            if not ok:
                report.add("warning", "text-braces", f"unbalanced braces in prose: {why}", q, fld)
            continue
        math = seg.content
        ok, why = latex.brace_balance(math)
        if not ok:
            report.add("error", "brace-balance", f"unbalanced braces in maths '{_short(math)}': {why}", q, fld)
        if not latex.left_right_balance(math):
            report.add("error", "left-right", f"\\left/\\right not paired in '{_short(math)}'", q, fld)
        for prob in latex.env_balance(math):
            report.add("error", "environment", prob, q, fld)
        stripped = latex.strip_text_arguments(math)
        for m in _BARE_ERR_RE.finditer(stripped):
            report.add("error", "bare-command", f"'{m.group(1)}' without backslash in maths '{_short(math)}'", q, fld)
        for m in _BARE_WARN_RE.finditer(stripped):
            report.add("warning", "bare-function",
                       f"'{m.group(1)}' without backslash in maths '{_short(math)}' (renders as italic letters)",
                       q, fld)
        if seg.kind == "inline" and "\n" in math:
            report.add("warning", "newline-in-inline-math", f"newline inside inline maths '{_short(math)}'", q, fld)
        if re.search(r"\\begin\s*\{", math):
            report.add("warning", "environment-used", "LaTeX environment used - prefer separate display lines if "
                       "the simulator mis-renders it", q, fld)


def _segment_kind_at(s: str, offset: int) -> str:
    segs, _ = latex.split_math(s)
    for seg in segs:
        if seg.start <= offset <= seg.start + len(seg.content):
            return seg.kind
    return "text"


def _short(s: str, n: int = 40) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _walk_keys(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, f"{path}.{k}" if path else k
            yield from _walk_keys(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


# --------------------------------------------------------------------------- images
def check_image(report: ValidationReport, img: Any, q: int, idx: int) -> None:
    fld = f"images[{idx}]"
    if not isinstance(img, dict):
        report.add("error", "image-type", "image entry must be an object", q, fld)
        return
    extra = set(img) - {"src", "alt"}
    if extra:
        report.add("error", "image-keys", f"unexpected image keys {sorted(extra)}", q, fld)
    alt = img.get("alt")
    if not isinstance(alt, str) or len(alt.strip()) < 8:
        report.add("error", "image-alt", "missing or too-short alt text", q, fld)
    src = img.get("src")
    if not isinstance(src, str) or not src.startswith("data:image/png;base64,"):
        report.add("error", "image-src", "src must be a data:image/png;base64 URI", q, fld)
        return
    try:
        data = decode_data_uri(src)
        pil = PILImage.open(io.BytesIO(data))
        pil.load()
    except Exception as exc:  # noqa: BLE001
        report.add("error", "image-decode", f"image does not decode as PNG: {exc}", q, fld)
        return
    w, h = pil.size
    if min(w, h) < 40:
        report.add("error", "image-size", f"image is tiny ({w}x{h}px)", q, fld)
    gray = pil.convert("L").point(lambda v: 255 if v < 225 else 0)
    bb = gray.getbbox()
    if bb is None:
        report.add("error", "image-blank", "image is blank", q, fld)
        return
    ink_frac = (bb[2] - bb[0]) * (bb[3] - bb[1]) / (w * h)
    if ink_frac < 0.45:
        report.add("warning", "image-whitespace", f"crop is mostly whitespace ({(1 - ink_frac):.0%} empty)", q, fld)
    # height in points, assuming the converter's output resolution
    if h / OUTPUT_ZOOM > 842 * 0.55:
        report.add("warning", "image-tall", "crop is taller than half a page - check it is not half the page", q, fld)


# --------------------------------------------------------------------------- paper
def validate_paper(data: Any, *, expected_questions: int | None = None, expected_duration: int | None = None,
                   pdf=None, katex_errors: dict[tuple[int, str], str] | None = None) -> ValidationReport:
    """Validate a parsed ``.tmua.json`` dict.

    ``pdf`` (a :class:`~tmua_converter.pdf.PdfDocument`) enables the sourcePage
    cross-check.  ``katex_errors`` maps (question, field) to a KaTeX error
    message, as produced by :mod:`tmua_converter.render`.
    """
    r = ValidationReport()
    if not isinstance(data, dict):
        r.add("error", "root", "top level must be a JSON object")
        return r

    keys = list(data)
    missing = [k for k in TOP_LEVEL_KEYS if k not in data]
    extra = [k for k in keys if k not in TOP_LEVEL_KEYS]
    if missing:
        r.add("error", "missing-keys", f"missing top-level keys: {missing}")
    if extra:
        r.add("error", "extra-keys", f"unexpected top-level keys: {extra}")
    for k, path in _walk_keys(data):
        if k.lower().replace("-", "") in FORBIDDEN_KEYS:
            r.add("error", "answer-leak", f"forbidden key '{path}' (no answers/solutions/marks allowed)")

    if data.get("formatVersion") != 1 or isinstance(data.get("formatVersion"), bool):
        r.add("error", "format-version", f"formatVersion must be the integer 1, got {data.get('formatVersion')!r}")
    for k in ("id", "title", "paper", "year"):
        check_string(r, data.get(k), None, k)
    if isinstance(data.get("id"), str) and re.search(r"\s", data["id"]):
        r.add("warning", "id-format", "id contains whitespace")

    dur = data.get("durationMinutes")
    if not isinstance(dur, int) or isinstance(dur, bool) or dur <= 0:
        r.add("error", "duration", f"durationMinutes must be a positive integer, got {dur!r}")
    elif expected_duration is not None and dur != expected_duration:
        r.add("error", "duration-mismatch", f"durationMinutes is {dur} but the paper states {expected_duration}")

    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        r.add("error", "questions", "questions must be a non-empty list")
        return r
    if expected_questions is not None and len(qs) != expected_questions:
        r.add("error", "question-count", f"{len(qs)} questions found but the paper has {expected_questions}")

    anchors = pdf.find_question_anchors() if pdf is not None else {}
    for pos, q in enumerate(qs, start=1):
        if not isinstance(q, dict):
            r.add("error", "question-type", f"question #{pos} is not an object")
            continue
        num = q.get("number")
        qn = num if isinstance(num, int) and not isinstance(num, bool) else pos
        if num != pos:
            r.add("error", "numbering", f"question at position {pos} has number {num!r} (must be sequential)", qn)
        qkeys = list(q)
        if [k for k in QUESTION_KEYS if k not in q]:
            r.add("error", "question-keys", f"missing keys {[k for k in QUESTION_KEYS if k not in q]}", qn)
        if [k for k in qkeys if k not in QUESTION_KEYS]:
            r.add("error", "question-keys", f"unexpected keys {[k for k in qkeys if k not in QUESTION_KEYS]}", qn)
        check_string(r, q.get("stem"), qn, "stem")

        opts = q.get("options")
        if not isinstance(opts, list) or not opts:
            r.add("error", "options", "question has no options", qn, "options")
            opts = []
        seen_content: dict[str, str] = {}
        for i, o in enumerate(opts):
            expected_label = chr(ord("A") + i)
            if not isinstance(o, dict):
                r.add("error", "option-type", f"option #{i + 1} is not an object", qn, "options")
                continue
            if set(o) != {"label", "content"}:
                r.add("error", "option-keys", f"option #{i + 1} keys must be label/content, got {sorted(o)}", qn,
                      "options")
            if o.get("label") != expected_label:
                r.add("error", "option-label", f"option #{i + 1} label is {o.get('label')!r}, expected "
                      f"{expected_label!r} (labels must be sequential A, B, C, ...)", qn, "options")
            content = o.get("content")
            check_string(r, content, qn, f"option {o.get('label', i + 1)}")
            if isinstance(content, str) and content.strip():
                key = re.sub(r"\s+", "", content)
                if key in seen_content:
                    r.add("warning", "duplicate-option",
                          f"options {seen_content[key]} and {o.get('label')} have identical content", qn, "options")
                seen_content.setdefault(key, str(o.get("label")))

        images = q.get("images")
        if not isinstance(images, list):
            r.add("error", "images", "images must be a list", qn, "images")
            images = []
        for i, img in enumerate(images):
            check_image(r, img, qn, i)
        stem = q.get("stem") if isinstance(q.get("stem"), str) else ""
        opt_text = " ".join(o.get("content", "") for o in opts if isinstance(o, dict) and isinstance(o.get("content"), str))
        if not images and _FIGURE_WORDS_RE.search(stem):
            r.add("warning", "figure-missing", "stem mentions a diagram/graph/table but the question has no image",
                  qn, "images")
        if not images and re.search(r"\bgraph\s*\(?[A-Ha-h]\)?", opt_text, re.I):
            r.add("error", "graph-options-missing", "options refer to graphs but no graph image is attached", qn,
                  "images")

        sp = q.get("sourcePage")
        if not isinstance(sp, int) or isinstance(sp, bool) or sp < 1:
            r.add("error", "source-page", f"sourcePage must be a positive integer, got {sp!r}", qn, "sourcePage")
        elif pdf is not None:
            if sp > pdf.page_count:
                r.add("error", "source-page", f"sourcePage {sp} exceeds the PDF's {pdf.page_count} pages", qn,
                      "sourcePage")
            elif qn in anchors and anchors[qn].page != sp:
                r.add("error", "source-page", f"sourcePage is {sp} but question {qn}'s number is printed on PDF "
                      f"page {anchors[qn].page}", qn, "sourcePage")
            elif qn not in anchors and pdf.has_text_layer():
                words = _words(stem)
                page_words = _words(pdf.text(sp))
                if words and len(words & page_words) / len(words) < 0.5:
                    r.add("warning", "source-page", f"less than half of the stem's words appear on page {sp}", qn,
                          "sourcePage")

        nr = q.get("needsReview")
        if not isinstance(nr, bool):
            r.add("error", "needs-review", "needsReview must be true/false", qn, "needsReview")
        elif nr:
            r.add("info", "needs-review", "flagged for manual review", qn, "needsReview")

    if katex_errors:
        for (qn, fld), msg in sorted(katex_errors.items()):
            r.add("error", "katex", f"KaTeX cannot render this: {msg}", qn, fld)
    return r


def _words(text: str) -> set[str]:
    text = re.sub(r"\$\$.*?\$\$|\$.*?\$", " ", text, flags=re.S)
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)}
