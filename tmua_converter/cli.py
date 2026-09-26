"""Command-line entry points: ``tmua-convert`` and ``tmua-validate``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .llm import SUPPORTED_MODELS
from .pipeline import ConversionResult, ConvertOptions
from .service import combine_results, convert_batch, make_runner, report_path_for, revalidate_file

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _summary_lines(res: ConversionResult) -> list[str]:
    p = res.paper
    s = res.summary()
    lines = [f"Conversion completed: {res.output_path}",
             f"  Questions: {len(p.questions)}   Duration: {p.durationMinutes} minutes"]
    checks = "pass 1 transcription, pass 2 independent verification"
    checks += " (with KaTeX render comparison)" if res.render_check_used else ""
    checks += ", pass 3 re-check of the written file"
    lines.append(f"  Every question visually cross-checked against the PDF pages: {checks}")
    lines.append(f"  Final file reloaded with a JSON parser and validated: {len(res.report.errors)} errors, "
                 f"{len(res.report.warnings)} warnings")
    imgs = s["questions_with_images"]
    lines.append(f"  Questions with source images: {', '.join(map(str, imgs)) if imgs else 'none'}")
    if s["needs_review"]:
        lines.append("  Needs review:")
        for n, reasons in s["needs_review"].items():
            lines.append(f"    Q{n}: {'; '.join(reasons)}")
    else:
        lines.append("  Needs review: none")
    for issue in res.report.issues:
        if issue.level in ("error", "warning"):
            where = f"Q{issue.question}" if issue.question else "paper"
            lines.append(f"  [{issue.level}] {where}: {issue.message}")
    for note in res.notes:
        lines.append(f"  Note: {note}")
    cost = res.usage.get("estimated_cost_usd")
    lines.append(f"  API usage: {res.usage['requests']} requests"
                 + (f", about ${cost:.2f}" if cost is not None else ""))
    lines.append(f"  Report: {report_path_for(res.output_path)}")
    return lines


def convert_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmua-convert",
                                 description="Convert exam paper PDFs into .tmua.json files (one per PDF).")
    ap.add_argument("pdfs", nargs="+", type=Path, help="PDF files, in order")
    ap.add_argument("-o", "--out-dir", type=Path, default=None, help="output folder (default: next to each PDF)")
    ap.add_argument("--combine", metavar="TITLE", help="also combine all papers, in the given order, into one file "
                                                       "with this exact title")
    ap.add_argument("--model", default=os.environ.get("TMUA_MODEL", "claude-opus-5"),
                    help=f"Claude model (default claude-opus-5; tested choices: {', '.join(SUPPORTED_MODELS)})")
    ap.add_argument("--effort", choices=EFFORTS, default=os.environ.get("TMUA_EFFORT", "high"))
    ap.add_argument("--concurrency", type=int, default=4, help="parallel Claude requests (default 4)")
    ap.add_argument("--no-render-check", action="store_true", help="skip the headless KaTeX render comparison")
    ap.add_argument("--title", help="override the paper title (single PDF only)")
    ap.add_argument("--paper", help="override the 'paper' field, e.g. 'Paper 1' (single PDF only)")
    ap.add_argument("--year", help="override the 'year' field (single PDF only)")
    ap.add_argument("--duration", type=int, help="override durationMinutes (single PDF only)")
    ap.add_argument("--questions", type=int, help="expected number of questions (single PDF only)")
    ap.add_argument("--filename", help="output file name (single PDF only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    for pdf in args.pdfs:
        if not pdf.is_file():
            ap.error(f"{pdf} not found")
    single = len(args.pdfs) == 1
    if not single and any([args.title, args.paper, args.year, args.duration, args.questions, args.filename]):
        ap.error("--title/--paper/--year/--duration/--questions/--filename apply to a single PDF")

    opts = ConvertOptions(model=args.model, effort=args.effort, concurrency=args.concurrency,
                          render_check=not args.no_render_check, title=args.title, paper=args.paper, year=args.year,
                          duration_minutes=args.duration, expected_questions=args.questions,
                          filename=args.filename)

    def progress(stage: str, msg: str) -> None:
        print(f"  [{stage}] {msg}", file=sys.stderr, flush=True)

    runner = make_runner(opts)
    status = 0
    # Each PDF goes next to itself unless an output folder is given.
    groups = [(args.out_dir, args.pdfs)] if args.out_dir else [(p.parent, [p]) for p in args.pdfs]
    all_results = []
    for out_dir, pdfs in groups:
        batch = convert_batch(pdfs, out_dir, opts, runner, combine_title=None, progress=progress)
        all_results.extend(batch.results)
        for name, err in batch.failures:
            print(f"FAILED: {name}: {err}", file=sys.stderr)
            status = 1
    for res in all_results:
        print("\n".join(_summary_lines(res)))
        print()
        if not res.report.ok:
            status = 1
    if args.combine and all_results and status == 0:
        out, report = combine_results(all_results, args.combine, args.out_dir or args.pdfs[0].parent)
        n = sum(len(r.paper.questions) for r in all_results)
        print(f"Combined file: {out} ({n} questions, in the order given) - reloaded and validated: "
              f"{len(report.errors)} errors, {len(report.warnings)} warnings")
        if not report.ok:
            print(report.format())
            status = 1
    elif args.combine:
        print("Not combining: fix the failures above first.", file=sys.stderr)
    return status


def validate_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmua-validate", description="Validate a .tmua.json file.")
    ap.add_argument("file", type=Path)
    ap.add_argument("--pdf", type=Path, help="source PDF, to cross-check sourcePage")
    ap.add_argument("--questions", type=int, help="expected number of questions")
    ap.add_argument("--duration", type=int, help="expected durationMinutes")
    args = ap.parse_args(argv)
    try:
        report = revalidate_file(args.file, args.pdf, args.questions, args.duration)
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        print(f"Could not load {args.file}: {exc}")
        return 2
    print(report.format())
    print(f"\n{len(report.errors)} errors, {len(report.warnings)} warnings")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(convert_main())
