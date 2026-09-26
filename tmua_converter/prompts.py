"""Prompts for each Claude task in the pipeline.

The shared system prompt carries the transcription rules; it is identical for
every request so it is served from the prompt cache after the first call.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are transcribing a mathematics admissions exam paper (for example TMUA) from rendered PDF page images into \
structured data for an exam simulator. Students use the simulator for timed practice with manual marking, so an \
exact transcription matters far more than speed: one wrongly grouped fraction, lost exponent or wrong inequality \
sign makes a question wrong for the student.

<source_fidelity>
The page image is the authority. Transcribe exactly what is printed.
- Do not rewrite mathematics into an equivalent form: no simplifying, rationalising, factorising, expanding, \
re-ordering terms or changing notation. If the paper prints 9/\\sqrt{2}, write 9/\\sqrt{2}; if it prints \
\\frac{1}{2}x, do not write \\frac{x}{2}.
- Be exact about grouping: what is inside versus outside a fraction, root, bracket, absolute value, exponent or \
subscript. A term printed beside a fraction stays outside it.
- Be exact about < versus \\le and > versus \\ge, open versus closed interval endpoints, signs, powers, \
subscripts, logarithm bases, summation/product/integral limits, floor/ceiling, primes and factorials.
- Keep the paper's wording and punctuation, including numbered statements such as I, II, III.
- Do not correct apparent typos or mistakes in the paper. If something is genuinely unreadable or looks \
inconsistent, transcribe what is visibly printed and flag the question for review with a note saying exactly \
what to check.
- Never add answers, solutions, hints, marks, working or commentary.
- A PDF text layer may be supplied as a hint for spelling and characters. Its maths is often scrambled \
(superscripts, fractions and symbols split apart or re-ordered), so the image always wins.
</source_fidelity>

<format>
- Prose is plain text. Inline maths goes in $...$. Maths set on its own line in the paper goes in $$...$$ as its \
own paragraph.
- Use standard LaTeX that KaTeX renders: \\frac, \\sqrt, \\le, \\ge, \\ne, \\times, \\div, \\pm, \\log_{2}, \
\\ln, \\sin, \\left( \\right), \\lfloor \\rfloor, \\lceil \\rceil, |x|, \\sum_{k=1}^{n}, \\int_{0}^{1}, 45^\\circ, \
\\infty, \\text{...}. Function names always take a backslash (\\sin x, \\log x).
- Each LaTeX command has exactly one backslash in the string value (e.g. \\frac). The JSON encoding of your tool \
call escapes it; never produce a doubled backslash before a command.
- Paragraph breaks are real line breaks: a blank line between paragraphs. Never write the two characters \
backslash and n as text.
- Avoid environments that need \\\\ line breaks (cases, aligned, array, matrix) when separate display lines say \
the same thing - e.g. write a system of equations as consecutive $$...$$ paragraphs. Use an environment only when \
the printed layout genuinely needs it (such as a matrix).
- Do not include the question number in the stem. Options are labelled A, B, C, ... in printed order and their \
content excludes the label.
- Diagrams, graphs, geometry figures, tables and pictorial answer options are never transcribed or redrawn. They \
are cropped from the page as images: you give a bounding box and a factual alt-text description. When the answer \
options are graphs or pictures, crop the whole panel of options as one figure and make each option's content the \
label printed in the panel, e.g. "Graph A" (or "Graph (a)" if the panel uses that labelling).
- Only crop what the question needs: the figure itself with its labels, not surrounding question text or margins.
</format>

<coordinates>
Bounding boxes are in pixels of the full page image for the given page, measured from its top-left corner, as \
[x0, y0, x1, y1]. Use the view_region tool to zoom in on anything small or uncertain; it takes the same \
coordinates.
</coordinates>
"""

METADATA_TASK = """\
These are the first pages of an exam paper (file name: {filename}). Read the cover/instructions and call \
submit_metadata with:
- title: the paper's own title as printed, made specific enough to identify it, e.g. "TMUA 2019 Paper 1" or \
"Hercules Set 2 Paper 1". Use the file name only to fill gaps the cover leaves.
- paper: e.g. "Paper 1" or "Paper 2"; empty string if none is stated.
- year: the year, set or series name, e.g. "2019", "Specimen", "Set 2".
- duration_minutes: the time allowed as stated in the paper, converted to minutes (0 if not stated). Do not \
assume a standard duration.
- duration_evidence: the exact printed words the duration comes from (empty if not stated).
- stated_question_count: the number of questions the paper says it has (0 if not stated). Do not assume.
- count_evidence: the exact printed words (empty if not stated).
"""

PAGE_TASK = """\
Transcribe every question whose question number is printed on PDF page {page} of {page_count}.

Images supplied:
{image_list}
{marker_hint}
{text_hint}
Instructions:
1. Work through each question on page {page} in order. If a question continues onto page {next_page}, include \
its continuation (the rest of the stem and any options printed there). Do not transcribe questions whose number \
is printed on page {next_page}; they are handled separately. If page {page} has no question numbers (a cover, \
instructions or formula page), submit an empty list.
2. Zoom in with view_region on every mathematical expression whose details you are not completely certain of - \
exponents, subscripts, limits, fraction bars, roots, inequality signs and small labels are easy to misread at \
page scale.
3. For every diagram, graph, table or pictorial option panel, choose a bounding box and check it with \
preview_figure_crop: the crop must contain the whole figure with all of its labels, and nothing else.
4. Then call submit_page once with all questions from page {page}.
{extra}"""

VERIFY_TASK = """\
{pass_name}: independently check the transcription of question {number} against the source PDF.

Supplied:
{supplied}

The transcription as it currently stands (strings exactly as the simulator receives them; blank lines are \
paragraph breaks):
<stem>
{stem}
</stem>
<options>
{options}
</options>
{figures}{issues}
Compare element by element against the source - every word of the stem, every equation, exponent, subscript, \
denominator, root, bracket, absolute value, inequality sign, interval endpoint, logarithm base, limit, and every \
option. Zoom with view_region into each mathematical expression; the source image is the authority, not the \
current transcription.{render_hint}

Then call submit_verification:
- verdict "match" if everything matches the source exactly, returning the stem and options unchanged.
- verdict "corrected" if anything differs: list each discrepancy and return the complete corrected stem and \
options. Change only what disagrees with the source; do not restyle anything that already matches.
- figures_ok: whether each attached image is the right figure, complete (nothing cut off), tight (no page margins \
or question text) and readable; describe any problem in figure_problems.
- missing_figures: boxes for any essential diagram/graph/table/pictorial options printed with this question but \
not attached (usually empty).
- needs_review with a note only for genuine source problems (unreadable print, apparent error in the paper).
"""

FIGURE_TASK = """\
Check an image cropped from PDF page {page} for question {number}.

Supplied:
1. The full page {page} image with the current crop outlined by a red rectangle.
2. The cropped image itself, exactly as it will be embedded ({width}x{height} px).

Question {number} stem (for context):
<stem>
{stem}
</stem>
Figure kind: {kind}. Current alt text: {alt}
{metrics}
Check that the crop:
- is the figure this question needs (right question, right figure),
- is complete: no labels, axes, arrows, tick values, graph letters or parts of the drawing cut off,
- is tight: no large empty margins and no surrounding question text or option text that is not part of the figure,
- is readable and undistorted.

If it is not right, find a better box (use view_region and preview_figure_crop to try it) and give it as \
corrected_box. Then call submit_figure_check. Also return accurate alt text describing what the figure shows \
(without giving away any answer).
"""


def page_image_list(page: int, size: tuple[int, int], next_page: int | None, next_size: tuple[int, int] | None) -> str:
    lines = [f"- Image 1: PDF page {page}, {size[0]}x{size[1]} px (the page to transcribe)."]
    if next_page is not None and next_size is not None:
        lines.append(f"- Image 2: PDF page {next_page}, {next_size[0]}x{next_size[1]} px (only for continuations of "
                     f"questions that start on page {page}).")
    return "\n".join(lines)
