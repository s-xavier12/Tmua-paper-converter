# TMUA Paper Converter

Upload an exam paper and get a `.tmua.json` file for your exam simulator.

It runs entirely on your own computer: **no AI, no API key, no account, no cost.** It works on:

- **digital PDFs**, where you can select the text (official TMUA papers, most mock papers): fully automatic.
- **pictures**: scanned PDFs, photos, screenshots, and PDFs with questions pasted in as screenshots.
  These are read with free offline OCR. OCR reads words well but gets maths symbols wrong, so every
  question read from a picture is marked `needsReview` for you to check against the picture.

## How it reads maths without AI

A digital PDF stores every character with its exact font, size and position, and draws fraction bars
and root bars as separate lines. The converter rebuilds the maths from that geometry:

| On the page | Becomes |
| --- | --- |
| characters above and below a drawn line | `\frac{…}{…}` |
| a √ sign with a bar over what follows | `\sqrt{…}` |
| smaller characters raised / dropped | `x^2`, `\log_2` |
| a big ∑ / ∫ with small text over and under it | `\sum_{k=1}^{10}` |
| tall brackets | `\left( … \right)` |
| maths fonts vs text fonts | `$…$` around the maths only |

Diagrams, graphs, tables and graph-option panels are cropped from the page itself, never redrawn.
Each crop is snapped to the drawing, keeps its labels, never takes in question text, and is trimmed
of whitespace.

## The checks every question goes through

1. **Rebuild.** Text and maths are rebuilt from the page layout. If anything is ambiguous, the
   question is flagged instead of guessed.
2. **Cross-check.** Every letter and digit printed in the question must appear in the output exactly
   once, and nothing extra may appear. A dropped exponent or a doubled character fails this check.
3. **Write, reload, validate.** The file is written, reloaded with a JSON parser, and validated. When
   the optional browser is installed, every expression is also rendered with KaTeX.

The validator checks:

- `formatVersion` is 1, and the duration and question count match what the paper says
- question numbers are sequential; options are labelled A, B, C, … with none missing
- no `correctAnswer`, solutions or marks
- no literal `\n` text, and no doubled LaTeX backslashes
- `$` delimiters balanced, using KaTeX's own rules; balanced `{}` and `\left`/`\right`
- no corrupted, invisible or control characters, and no `frac`/`sqrt` missing its backslash
- images are real PNGs with alt text, and `sourcePage` matches the page the question number is printed on

Anything unresolved gets `"needsReview": true`, with the reason in the summary and on the review page.

The tests typeset a sample paper three ways: LaTeX fonts, Times, and Word-style Unicode maths with
options across the line. Every question comes out **exactly** right in all three.

## Setup

1. Install [Python 3.10+](https://www.python.org/downloads/).
2. In a terminal, in this folder:

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -e .
   ```

3. **Optional, for pictures (scans, photos, screenshots):** install the free Tesseract OCR program.
   - Windows: the installer from https://github.com/UB-Mannheim/tesseract/wiki
   - macOS: `brew install tesseract`
   - Linux: `sudo apt install tesseract-ocr`

   Digital PDFs don't need it.
4. **Optional, KaTeX render check:** `pip install -e ".[render]"` then `playwright install chromium`.

## Web app

```bash
tmua-app          # then open http://127.0.0.1:8000
```

1. Drop in PDFs and/or pictures and put them in order.
   - With several pictures, tick **The pictures are the pages of one paper** if they belong together.
   - You can override the title, paper, year, time or question count, but you don't have to.
2. Tick **Also combine** and enter a title if you want one combined paper. The papers keep your
   order and the questions are renumbered.
3. Press **Convert**. A digital PDF takes a few seconds; pictures take longer.
4. Each paper shows its question count, the checks it passed, which questions have source images,
   and which need review. There's a download link for each `.tmua.json`.
5. **Review & edit** shows each question's source next to how the simulator will show it. You can
   edit any stem, option, alt text or flag, then **Save & re-validate** to write, reload and validate
   the file again.

You can also open any existing `.tmua.json` (with its PDF, optionally) to check and edit it.

## Command line

```bash
tmua-convert "Hercules Set 2 Paper 1.pdf"               # -> Hercules_Set_2_Paper_1.tmua.json
tmua-convert p1.pdf p2.pdf -o out                       # one file per paper
tmua-convert 2019_P1.pdf 2020_P1.pdf -o out --combine "TMUA Mixed Mock 1"
tmua-convert page1.jpg page2.jpg page3.jpg --pages      # photos of one paper's pages
tmua-convert paper.pdf --duration 75 --title "Hercules Set 2" --paper "Paper 1" --year "Set 2"
tmua-validate Hercules_Set_2_Paper_1.tmua.json --pdf "Hercules Set 2 Paper 1.pdf"
```

Each run prints:

- that the conversion completed, with the number of questions
- that every question was checked
- that the written file was reloaded and validated
- which questions contain source images
- which questions need review, and why

A `.report.json` next to each file has the full details.

## Good to know

- Durations and question counts are read from the cover ("Time allowed: 1 hour 15 minutes" → 75). If
  the paper doesn't state a time, the file uses 75 minutes and the summary says so. Change it with
  `--duration` or on the review page.
- Printed typos are kept as printed. Anything unusual is flagged, never "corrected".
- Layouts the converter doesn't recognise are flagged rather than guessed: for example matrices, or a
  question whose options it can't find. Always look at the questions marked **needs review**.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
python tests/make_latex_papers.py    # regenerate the test papers (needs pdflatex + xelatex)
```

KaTeX 0.16.22 is vendored under `tmua_converter/web/static/vendor/katex` (MIT licence).
