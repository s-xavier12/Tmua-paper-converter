"""High-level operations shared by the CLI and the web app."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .combine import combine_papers
from .naming import paper_filename
from .ocr import images_to_pdf, is_image_file
from .pdf import PdfDocument
from .pipeline import ConversionResult, ConvertOptions, Converter
from .schema import Paper, load_paper_dict, write_paper
from .validator import ValidationReport, validate_paper


@dataclass
class BatchResult:
    results: list[ConversionResult] = field(default_factory=list)
    combined_path: Path | None = None
    combined_report: ValidationReport | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)  # (input name, error)


def group_inputs(paths: list[Path], images_are_one_paper: bool, work_dir: Path) -> list[Path]:
    """Each PDF is a paper; pictures are one paper each, or together the pages of one paper."""
    images = [p for p in paths if is_image_file(p)]
    if images_are_one_paper and len(images) > 1:
        work_dir.mkdir(parents=True, exist_ok=True)
        merged = images_to_pdf(images, work_dir / f"{images[0].stem}_pages.source.pdf")
        out, done = [], False
        for p in paths:
            if is_image_file(p):
                if not done:
                    out.append(merged)
                    done = True
            else:
                out.append(p)
        return out
    return list(paths)


def report_path_for(out_path: Path) -> Path:
    name = out_path.name
    if name.endswith(".tmua.json"):
        name = name[: -len(".tmua.json")]
    return out_path.with_name(name + ".report.json")


def write_report(result: ConversionResult) -> Path:
    """Sidecar with the validation report, review reasons and check history."""
    data = result.summary()
    data["metadata_evidence"] = result.metadata
    data["questions_detail"] = {
        str(n): {"sourcePage": s.source_page, "needsReview": s.needs_review, "reviewReasons": s.review_reasons,
                 "history": s.history, "regions": [[p, b.to_list()] for p, b in s.regions],
                 "figures": [{"page": f.crop.page, "box_pt": f.crop.box_pt.to_list(), "notes": f.crop.notes}
                             for f in s.figures]}
        for n, s in sorted(result.questions.items())
    }
    path = report_path_for(result.output_path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def convert_batch(inputs: list[Path], out_dir: Path, options: ConvertOptions, combine_title: str | None = None,
                  progress: Callable[[str, str], None] | None = None,
                  per_paper_options: list[ConvertOptions] | None = None) -> BatchResult:
    """Convert each input to its own file; optionally combine them in the given order."""
    progress = progress or (lambda stage, msg: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    batch = BatchResult()
    used: set[str] = set()
    for i, path in enumerate(inputs):
        opts = per_paper_options[i] if per_paper_options and i < len(per_paper_options) else options
        opts = replace(opts, avoid_names=frozenset(used))
        progress("paper", f"Paper {i + 1}/{len(inputs)}: {path.name}")
        try:
            res = Converter(opts, progress).convert(path, out_dir)
        except Exception as exc:  # noqa: BLE001 - reported per paper
            batch.failures.append((path.name, f"{exc}"))
            progress("error", f"{path.name} failed: {exc}")
            continue
        write_report(res)
        used.add(res.output_path.name)
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
