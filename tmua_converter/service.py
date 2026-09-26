"""High-level operations shared by the CLI and the web app."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .combine import combine_papers
from .llm import AnthropicTransport, ClaudeRunner
from .naming import paper_filename
from .pdf import PdfDocument
from .pipeline import ConversionResult, ConvertOptions, Converter
from .schema import Paper, load_paper_dict, write_paper
from .validator import ValidationReport, validate_paper


@dataclass
class BatchResult:
    results: list[ConversionResult] = field(default_factory=list)
    combined_path: Path | None = None
    combined_report: ValidationReport | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)  # (pdf name, error)


def make_runner(options: ConvertOptions, api_key: str | None = None, transport=None) -> ClaudeRunner:
    transport = transport or AnthropicTransport(api_key=api_key)
    return ClaudeRunner(transport, model=options.model, effort=options.effort)


def report_path_for(out_path: Path) -> Path:
    name = out_path.name
    if name.endswith(".tmua.json"):
        name = name[: -len(".tmua.json")]
    return out_path.with_name(name + ".report.json")


def write_report(result: ConversionResult) -> Path:
    """Sidecar with the validation report, review reasons and correction history."""
    data = result.summary()
    data["metadata_evidence"] = result.metadata
    data["questions_detail"] = {
        str(n): {"sourcePage": s.source_page, "needsReview": s.needs_review, "reviewReasons": s.review_reasons,
                 "history": s.history, "regions": [[p, b.to_list()] for p, b in s.regions],
                 "figures": [{"page": f.spec.page, "box_pt": f.crop.box_pt.to_list(), "confirmed": f.confirmed,
                              "notes": f.notes + f.crop.notes} for f in s.figures]}
        for n, s in sorted(result.questions.items())
    }
    path = report_path_for(result.output_path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def convert_batch(pdfs: list[Path], out_dir: Path, options: ConvertOptions, runner: ClaudeRunner,
                  combine_title: str | None = None, progress: Callable[[str, str], None] | None = None,
                  per_paper_options: list[ConvertOptions] | None = None) -> BatchResult:
    """Convert each PDF to its own file; optionally combine them in the given order."""
    progress = progress or (lambda stage, msg: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    batch = BatchResult()
    for i, pdf in enumerate(pdfs):
        opts = per_paper_options[i] if per_paper_options else options
        progress("paper", f"Paper {i + 1}/{len(pdfs)}: {pdf.name}")
        try:
            res = Converter(runner, opts, progress).convert(pdf, out_dir)
        except Exception as exc:  # noqa: BLE001 - reported per paper
            if type(exc).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                raise
            batch.failures.append((pdf.name, f"{type(exc).__name__}: {exc}"))
            progress("error", f"{pdf.name} failed: {exc}")
            continue
        write_report(res)
        batch.results.append(res)
    if combine_title and batch.results and not batch.failures:
        batch.combined_path, batch.combined_report = combine_results(batch.results, combine_title, out_dir)
        progress("combine", f"Combined {len(batch.results)} papers into {batch.combined_path.name}")
    elif combine_title and batch.failures:
        progress("combine", "Not combining because at least one paper failed")
    return batch


def combine_results(results: list[ConversionResult], title: str, out_dir: Path) -> tuple[Path, ValidationReport]:
    """Combine converted papers (in order) into one file; write, reload and validate it."""
    combined = combine_papers([r.paper for r in results], title)
    path = out_dir / paper_filename(title)
    write_paper(combined, path)
    report = validate_paper(load_paper_dict(path), expected_questions=len(combined.questions),
                            expected_duration=combined.durationMinutes)
    return path, report


def revalidate_file(path: Path, pdf: Path | None = None, expected_questions: int | None = None,
                    expected_duration: int | None = None) -> ValidationReport:
    data = load_paper_dict(path)
    if pdf is not None:
        with PdfDocument(pdf) as doc:
            return validate_paper(data, expected_questions=expected_questions, expected_duration=expected_duration,
                                  pdf=doc)
    return validate_paper(data, expected_questions=expected_questions, expected_duration=expected_duration)


def save_edited_paper(data: dict, path: Path) -> Paper:
    """Validate the structure of an edited paper and write it (raises on schema errors)."""
    paper = Paper.model_validate(data)
    write_paper(paper, path)
    return paper
