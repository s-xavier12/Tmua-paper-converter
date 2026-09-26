# TMUA Paper Converter

Upload an exam paper PDF and get a `.tmua.json` file for your exam simulator. The app is built for
accuracy over speed: every question is transcribed, checked against the page images at least twice,
written to disk, reloaded, validated and checked again. Figures are cropped from the PDF itself, never
redrawn.

It runs on your own computer, as a web app (upload, watch progress, review, download) or from the
command line. Transcription uses Claude (Anthropic API), so you need an API key.

## What it produces

```json
{
  "formatVersion": 1,
  "id": "hercules-set-2-paper-1",
  "title": "Hercules Set 2 Paper 1",
  "paper": "Paper 1",
  "year": "Set 2",
  "durationMinutes": 75,
  "questions": [
    {
      "number": 1,
      "stem": "Find the complete set of values of $x$ for which\n\n$$\\frac{x^2 - 4}{x + 1} \\le 0$$",
      "options": [{ "label": "A", "content": "$x \\le -2$" }, "..."],
      "images": [],
      "sourcePage": 2,
      "needsReview": false
    }
  ]
}
```

- The question count and duration come from the paper itself; nothing assumes 20 questions or 75 minutes.
  The one exception: if the paper doesn't state a time at all, the file uses 75 minutes and the summary
  tells you so. Override it with `--duration` or on the review page.
- The file never contains `correctAnswer`, solutions, explanations or marks.
- After `JSON.parse`, paragraph breaks are real newlines and each LaTeX command has exactly one backslash.
  The raw file shows them escaped as `\n` and `\\frac`, which is normal JSON.
- One file per PDF, named after the paper, e.g. `Hercules_Set_2_Paper_1.tmua.json`. You can also combine
  several papers into one, in the order you give, with the exact title you give.

## How a paper is converted

1. **Metadata.** Claude reads the cover page for the title, paper, year or set, the stated time
   allowed, and the stated number of questions.
2. **Pass 1: transcription.** Each page is rendered at high resolution and sent to Claude with the
   following page (for questions that run over) and the PDF text layer as a spelling hint. Claude can
   zoom into any region to read exponents, limits, fraction bars and inequality signs, and can preview
   each figure crop before it submits. Submissions with broken LaTeX (unbalanced `$` or braces, a
   literal `\n`, doubled backslashes, control characters, …) are sent back to be fixed. If a question
   number is missing, its page is read again.
3. **Figures.** Every diagram, graph, table and graphical option panel is cropped from the rendered
   page. The crop snaps to the actual drawing, pulls in its labels, never grows into question text,
   and is trimmed of whitespace. Claude then inspects each crop, shown outlined on the page, and
   corrects the box if anything is cut off, too loose or wrong.
4. **Pass 2: verification.** A fresh Claude call checks each question against a zoomed crop of the
   source and a KaTeX render of the current JSON, so it sees exactly what a student will see. Grouping
   mistakes (a term inside or outside a fraction, a lost exponent) show up clearly in the render.
   Corrections are re-verified until a check finds nothing left to fix.
5. **Write, reload, validate.** The file is written, reloaded with a JSON parser, and run through the
   validator below. Claude also renders each question with KaTeX and reports anything that fails to
   render.
6. **Pass 3: final check.** Every question is checked again, this time using the strings from the
   reloaded file. The file is then written, reloaded and validated one last time, and that report is
   the one shown.

Anything that doesn't settle gets `"needsReview": true`, and the summary says exactly what to check.
That covers a source that looks inconsistent or unreadable (the printed text is kept, never
"corrected"), verifiers that keep disagreeing, and figure crops that couldn't be confirmed.

### Validator checks (run on the reloaded file)

`formatVersion` is 1 · duration and question count match the paper · numbers are sequential ·
every question has options labelled A, B, C, … with nothing missing · no answer/solution/marks keys ·
no literal `\n` text · no doubled backslashes · balanced `$`/`$$`, using the exact rules KaTeX
auto-render uses (for example, `\$` is *not* an escape) · balanced `{}`, `\left`/`\right` and environments ·
no control, invisible, private-use or replacement characters (this catches `\times` or `\frac` that
turned into a tab or form feed) · no bare `frac`/`sqrt`/`infty`/… missing a backslash · no LaTeX outside
`$…$` · every expression renders in KaTeX · images are real PNG data URIs with alt text and aren't mostly
whitespace · `sourcePage` matches the page where the question number is printed in the PDF.

## Setup

You need Python 3.10+ and an [Anthropic API key](https://console.anthropic.com/).

```bash
git clone https://github.com/s-xavier12/Tmua-paper-converter.git
cd Tmua-paper-converter
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[render]"
playwright install chromium        # enables the KaTeX render comparison (recommended)
export ANTHROPIC_API_KEY=sk-ant-...  # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
```

Without Playwright and Chromium everything still works, but the verifier compares LaTeX source with
the page instead of a rendered preview.

## Web app

```bash
tmua-app            # then open http://127.0.0.1:8000
```

1. Drop in one or more PDFs. Reorder them if you plan to combine them. You can override the title,
   paper, year, duration or question count per PDF, but you don't have to.
2. Tick **Also combine** and enter a title if you want one combined paper.
3. Press **Convert** and leave the tab open. The progress log shows each pass.
4. When it finishes, each paper shows its question count, the cross-check and validation results,
   which questions have source images, and which need review. You get direct download links for each
   `.tmua.json`.
5. **Review & edit** puts each question's PDF source next to how the simulator will show it, along
   with validator findings and the check history. You can edit any stem, option, alt text or flag.
   **Save & re-validate** writes the file, reloads it and validates it again.

You can also open an existing `.tmua.json` (with its PDF, optionally) to validate, preview and edit it.
Uploads and outputs are kept in `./tmua_output/` (change it with `--workdir`). The app only listens on
your own machine (`127.0.0.1`).

## Command line

```bash
# one paper -> Hercules_Set_2_Paper_1.tmua.json (next to the PDF) + a .report.json
tmua-convert "Hercules Set 2 Paper 1.pdf"

# two papers -> one file each, in ./out
tmua-convert p1.pdf p2.pdf -o out

# also combine them, in this order, with an exact title
tmua-convert 2019_P1.pdf 2020_P1.pdf -o out --combine "TMUA Mixed Mock 1"

# override what the cover page says (single PDF)
tmua-convert paper.pdf --title "Hercules Set 2" --paper "Paper 1" --year "Set 2" --duration 75 --questions 20

# check any .tmua.json (optionally cross-checking sourcePage against its PDF)
tmua-validate Hercules_Set_2_Paper_1.tmua.json --pdf "Hercules Set 2 Paper 1.pdf"
```

The final summary lists: conversion completed, number of questions, confirmation that every question
was visually cross-checked, that the written file was reloaded and validated, which questions contain
source images, and which need review.

## Model, cost and time

- The default model is `claude-opus-5` with adaptive thinking at `high` effort. Choose another with
  `--model`/`--effort` or under **Model settings**. `--effort max` is the most thorough and the slowest.
- Server-side refusal fallback (`fallbacks: "default"`) is switched on. If Claude's safety classifiers
  decline a request (rare for maths papers), the API retries it on Anthropic's recommended fallback model
  instead of failing.
- Each question is read at least three times and each figure is inspected at least once, so a
  20-question paper makes roughly 60–100 API calls. Expect it to take tens of minutes and to cost a few
  dollars up to low tens of dollars, depending on the model, the effort level and how many figures the
  paper has. Every run prints its actual token usage and an estimated cost.

## Tips and limitations

- Text-based PDFs (like the official TMUA papers) work best. The text layer lets the app find each
  question's printed number, which gives exact question regions and a deterministic `sourcePage`
  check. Scanned PDFs still work, but those steps then rely on Claude.
- Always look at the questions marked **needs review**. The review page puts each one next to its source.
- Transcription quality comes from Claude. The app's job is to make mistakes unlikely (several
  independent checks) and visible when they happen (validator, render comparison, review flags).

## Development

```bash
pip install -e ".[dev]"
python -m pytest            # uses a generated sample paper and a scripted stand-in for Claude
python tests/make_sample_paper.py   # regenerate tests/data/sample_paper.pdf
```

The tests cover the validator, PDF/question detection, figure cropping, the Claude tool loop, the real
SDK request/stream handling (against a local mock API), the full pipeline, the web API and the CLI.
KaTeX 0.16.22 is vendored under `tmua_converter/web/static/vendor/katex` (MIT licence).
