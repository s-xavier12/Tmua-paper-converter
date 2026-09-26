"""Command-line entry points: ``tmua-convert`` and ``tmua-validate``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .ocr import IMAGE_SUFFIXES
from .pipeline import ConversionResult, ConvertOptions
from .service import combine_results, convert_batch, group_inputs, report_path_for, revalidate_file


def _summary_lines(res: ConversionResult) -> list[str]:
    p = res.paper
    s = res.summary()
    lines = [f"Conversion completed: {res.output_path}",
             f"  Questions: {len(p.questions)}   Duration: {p.durationMinutes} minutes"]
    checks = "rebuilt from the page layout, cross-checked character by character against the PDF"
    if res.render_check_used:
        checks += ", every expression rendered with KaTeX"
    lines.append(f"  Every question checked: {checks}")
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
    lines.append(f"  Report: {report_path_for(res.output_path)}")
    return lines


def convert_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tmua-convert",
        description="Convert exam papers (PDFs, scans or photos) into .tmua.json files - one per paper. "
                    "Runs entirely on your computer: no AI, no account, no cost.")
    ap.add_argument("inputs", nargs="+", type=Path,
                    help=f"PDF files or pictures ({', '.join(sorted(IMAGE_SUFFIXES))}), in order")
    ap.add_argument("-o", "--out-dir", type=Path, default=None, help="output folder (default: next to each input)")
    ap.add_argument("--pages", action="store_true",
                    help="the pictures given are the pages of ONE paper, in order (default: one paper per picture)")
    ap.add_argument("--combine", metavar="TITLE", help="also combine all papers, in the given order, into one file "
                                                       "with this exact title")
    ap.add_argument("--no-render-check", action="store_true", help="skip the headless KaTeX render check")
    ap.add_argument("--title", help="override the paper title (single paper only)")
    ap.add_argument("--paper", help="override the 'paper' field, e.g. 'Paper 1' (single paper only)")
    ap.add_argument("--year", help="override the 'year' field (single paper only)")
    ap.add_argument("--duration", type=int, help="override durationMinutes (single paper only)")
    ap.add_argument("--questions", type=int, help="expected number of questions (single paper only)")
    ap.add_argument("--filename", help="output file name (single paper only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    for path in args.inputs:
        if not path.is_file():
            ap.error(f"{path} not found")
    out_root = args.out_dir or args.inputs[0].parent
    inputs = group_inputs(args.inputs, args.pages, out_root)
    if len(inputs) > 1 and any([args.title, args.paper, args.year, args.duration, args.questions, args.filename]):
        ap.error("--title/--paper/--year/--duration/--questions/--filename apply to a single paper")

    opts = ConvertOptions(render_check=not args.no_render_check, title=args.title, paper=args.paper, year=args.year,
                          duration_minutes=args.duration, expected_questions=args.questions, filename=args.filename)

    def progress(stage: str, msg: str) -> None:
        print(f"  [{stage}] {msg}", file=sys.stderr, flush=True)

    status = 0
    groups = [(args.out_dir, inputs)] if args.out_dir else [(p.parent, [p]) for p in inputs]
    results = []
    for out_dir, items in groups:
        batch = convert_batch(items, out_dir, opts, progress=progress)
        results.extend(batch.results)
        for name, err in batch.failures:
            print(f"FAILED: {name}: {err}", file=sys.stderr)
            status = 1
    for res in results:
        print("\n".join(_summary_lines(res)))
        print()
        if not res.report.ok:
            status = 1
    if args.combine and results and status == 0:
        out, report = combine_results(results, args.combine, out_root)
        n = sum(len(r.paper.questions) for r in results)
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
