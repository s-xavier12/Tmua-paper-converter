"""A scripted stand-in for the Claude API, driven by the sample paper's ground truth.

It answers each pipeline task (recognised by its submit tool) the way a careful
model would, and can inject realistic mistakes so the tests can check that the
pipeline's validation and verification loops catch and fix them.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from make_sample_paper import EXPECTED

from tmua_converter.pdf import PdfDocument

# Where the figures are on the sample PDF, in PDF points (page, x0, y0, x1, y1).
# Deliberately a bit loose/tight - the cropper has to fix them.
FIGURE_BOXES_PT = {
    2: (2, 150, 400, 330, 505),  # triangle: slightly too tight on the left, labels partly outside
    3: (3, 120, 100, 420, 345),  # graph panel
}


@dataclass
class Script:
    """Knobs for injected mistakes."""

    pass1_wrong_inequality: bool = True   # Q1 transcribed with '<' instead of '\le' in pass 1
    pass1_literal_newline_first_try: bool = True  # page 5 first submission uses literal backslash-n
    view_region_first: bool = True  # page 3 zooms before submitting
    verifier_always_corrects: set[int] = field(default_factory=set)  # never converges for these questions


def _q(num: int) -> dict:
    return next(q for q in EXPECTED["questions"] if q["number"] == num)


def _opts(num: int) -> list[dict]:
    return [{"label": chr(65 + i), "content": c} for i, c in enumerate(_q(num)["options"])]


class FakeTransport:
    custom_base_url = False

    def __init__(self, pdf_path, script: Script | None = None):
        self.doc = PdfDocument(pdf_path)
        self.script = script or Script()
        self.calls: list[dict] = []
        self._ids = itertools.count(1)
        self._page5_attempts = 0

    # --------------------------------------------------------------- plumbing
    def _tool_use(self, name: str, inp: dict):
        return SimpleNamespace(type="tool_use", id=f"toolu_{next(self._ids)}", name=name, input=inp)

    def _msg(self, *blocks):
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        content = [SimpleNamespace(type="thinking", thinking="", signature="sig")] + list(blocks)
        return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage, stop_details=None)

    @staticmethod
    def _prompt_text(params) -> str:
        first = params["messages"][0]["content"]
        return "\n".join(b["text"] for b in first if b.get("type") == "text")

    @staticmethod
    def _last_tool_results(params) -> list[dict]:
        last = params["messages"][-1]
        if last["role"] != "user" or not isinstance(last["content"], list):
            return []
        return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]

    def _px(self, page: int, box_pt):
        z = self.doc.model_zoom(page)
        return {"page": page, "x0": box_pt[0] * z, "y0": box_pt[1] * z, "x1": box_pt[2] * z, "y1": box_pt[3] * z}

    # --------------------------------------------------------------- dispatch
    def send(self, params: dict):
        self.calls.append(params)
        tools = {t["name"] for t in params["tools"]}
        text = self._prompt_text(params)
        results = self._last_tool_results(params)
        if "submit_metadata" in tools:
            return self._msg(self._tool_use("submit_metadata", {
                "title": EXPECTED["title"], "paper": EXPECTED["paper"], "year": "2026",
                "duration_minutes": EXPECTED["durationMinutes"], "duration_evidence": "Time allowed: 40 minutes",
                "stated_question_count": 4, "count_evidence": "There are four questions in this paper."}))
        if "submit_page" in tools:
            page = int(re.search(r"printed on PDF page (\d+)", text).group(1))
            return self._page(page, params, results)
        if "submit_figure_check" in tools:
            box = re.search(r"crop box \[([\d.\s,]+)\] px", text).group(1)
            x0, y0, x1, y1 = (float(v) for v in box.split(","))
            page = int(re.search(r"cropped from PDF page (\d+)", text).group(1))
            num = int(re.search(r"for question (\d+)", text).group(1))
            return self._msg(self._tool_use("submit_figure_check", {
                "ok": True, "problems": [], "corrected_box": {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                                                              "exact": False},
                "alt": f"Figure for question {num} as printed in the paper", "note": ""}))
        if "submit_verification" in tools:
            num = int(re.search(r"transcription of question (\d+)", text).group(1))
            return self._verify(num, text)
        raise AssertionError(f"unexpected task with tools {tools}")

    def _page(self, page: int, params, results):
        if page == 3 and self.script.view_region_first and not results:
            return self._msg(self._tool_use("view_region", {"page": 3, "x0": 300, "y0": 200, "x1": 1600, "y1": 1100}))
        questions = []
        for q in EXPECTED["questions"]:
            if q["page"] != page:
                continue
            stem = q["stem"]
            if q["number"] == 1 and self.script.pass1_wrong_inequality:
                stem = stem.replace("\\le 0", "< 0")
            if page == 5 and self.script.pass1_literal_newline_first_try and self._page5_attempts == 0:
                stem = stem.replace("\n\n", "\\n\\n")
            figs = []
            if q["number"] in FIGURE_BOXES_PT:
                fp, *box = FIGURE_BOXES_PT[q["number"]]
                kind = "answer_options_panel" if q.get("figure") == "graph_options" else "diagram"
                figs.append({**self._px(fp, box), "kind": kind, "alt": f"Figure for question {q['number']}"})
            questions.append({"number": q["number"], "stem": stem, "options": _opts(q["number"]), "figures": figs,
                              "region": {"y0": 0, "y1": 0, "continues_on_next_page": False, "continuation_y1": 0},
                              "needs_review": False, "review_note": ""})
        if page == 5:
            self._page5_attempts += 1
        return self._msg(self._tool_use("submit_page", {"questions": questions, "page_note": ""}))

    def _verify(self, num: int, text: str):
        truth = _q(num)
        current_stem = re.search(r"<stem>\n(.*?)\n</stem>", text, re.S).group(1)
        stem = truth["stem"]
        if num in self.script.verifier_always_corrects:
            stem = stem + (" " if not current_stem.endswith(" ") else "")  # keeps flip-flopping
        verdict = "match" if current_stem == stem else "corrected"
        disc = [] if verdict == "match" else [{"where": "stem", "source_shows": "\\le",
                                               "transcription_had": "<"}]
        return self._msg(self._tool_use("submit_verification", {
            "verdict": verdict, "discrepancies": disc, "stem": stem, "options": _opts(num), "figures_ok": True,
            "figure_problems": "", "missing_figures": [], "needs_review": False, "review_note": ""}))
