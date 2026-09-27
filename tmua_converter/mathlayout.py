"""Rebuilding maths from the PDF's own layout - no AI, no OCR.

A text-based PDF stores every character with its font, size, position and
baseline, and draws fraction bars and root bars as separate lines.  From that
geometry alone the structure of an expression can be recovered:

* a line with characters directly above and below it          -> a fraction
* a radical sign with a bar running over what follows           -> a square root
* a smaller character raised above / dropped below the baseline -> ^ / _
* a large sum / product / integral with small text over/under   -> limits
* fonts tell maths apart from prose (e.g. CMMI = maths italic)

This module turns the characters and bars of one block of the page into
LaTeX-in-text strings ("Find $x$ for which $$\\frac{x^2 - 4}{x + 1} \\le 0$$").
Anything it cannot interpret with confidence is reported in ``problems`` so
the question is flagged for review instead of silently guessed.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import replace, dataclass, field

# --------------------------------------------------------------------------- character tables
GREEK = {
    "α": "\\alpha", "β": "\\beta", "γ": "\\gamma", "δ": "\\delta", "ε": "\\varepsilon", "ϵ": "\\epsilon",
    "ζ": "\\zeta", "η": "\\eta", "θ": "\\theta", "ϑ": "\\vartheta", "ι": "\\iota", "κ": "\\kappa",
    "λ": "\\lambda", "μ": "\\mu", "ν": "\\nu", "ξ": "\\xi", "π": "\\pi", "ϖ": "\\varpi", "ρ": "\\rho",
    "ϱ": "\\varrho", "σ": "\\sigma", "ς": "\\varsigma", "τ": "\\tau", "υ": "\\upsilon", "φ": "\\varphi",
    "ϕ": "\\phi", "χ": "\\chi", "ψ": "\\psi", "ω": "\\omega", "Γ": "\\Gamma", "Δ": "\\Delta", "Θ": "\\Theta",
    "Λ": "\\Lambda", "Ξ": "\\Xi", "Π": "\\Pi", "Σ": "\\Sigma", "Υ": "\\Upsilon", "Φ": "\\Phi", "Ψ": "\\Psi",
    "Ω": "\\Omega",
}
SYMBOLS = {
    "−": "-", "–": "-", "±": "\\pm", "∓": "\\mp", "×": "\\times", "÷": "\\div", "·": "\\cdot", "⋅": "\\cdot",
    "∗": "*", "≤": "\\le", "≥": "\\ge", "≠": "\\ne", "≈": "\\approx", "≡": "\\equiv", "∼": "\\sim",
    "≃": "\\simeq", "≅": "\\cong", "∝": "\\propto", "⩽": "\\leqslant", "⩾": "\\geqslant", "≪": "\\ll",
    "≫": "\\gg", "∞": "\\infty", "∂": "\\partial", "∇": "\\nabla", "∀": "\\forall", "∃": "\\exists",
    "∈": "\\in", "∉": "\\notin", "∋": "\\ni", "⊂": "\\subset", "⊆": "\\subseteq", "⊃": "\\supset",
    "⊇": "\\supseteq", "∪": "\\cup", "∩": "\\cap", "∅": "\\emptyset", "→": "\\to", "←": "\\leftarrow",
    "⇒": "\\Rightarrow", "⇐": "\\Leftarrow", "⇔": "\\Leftrightarrow", "↔": "\\leftrightarrow",
    "⟹": "\\Longrightarrow", "⟸": "\\Longleftarrow", "⟺": "\\Longleftrightarrow", "∠": "\\angle",
    "△": "\\triangle", "∆": "\\triangle", "…": "\\ldots", "⋯": "\\cdots", "∘": "\\circ", "◦": "\\circ",
    "∣": "|", "‖": "\\|", "⌊": "\\lfloor", "⌋": "\\rfloor", "⌈": "\\lceil", "⌉": "\\rceil",
    "⟨": "\\langle", "⟩": "\\rangle", "〈": "\\langle", "〉": "\\rangle", "¬": "\\neg", "∧": "\\wedge",
    "∨": "\\vee", "⊥": "\\perp", "∥": "\\parallel", "∑": "\\sum", "∏": "\\prod", "∫": "\\int",
    "∮": "\\oint", "′": "'", "″": "''", "∴": "\\therefore", "∵": "\\because", "ℝ": "\\mathbb{R}",
    "ℤ": "\\mathbb{Z}", "ℕ": "\\mathbb{N}", "ℚ": "\\mathbb{Q}", "ℂ": "\\mathbb{C}", "{": "\\{", "}": "\\}",
    "%": "\\%", "#": "\\#", "&": "\\&", "£": "\\pounds", "$": "\\$",
}
PLAIN_MATH = set("=<>+*|")
UNI_SUP = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", "0123456789+−=()ni"))
UNI_SUB = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+−=()"))
PUNCT = set(",.;:?!()[]'\"‘’“”/")
BIG_OPS = {"∑": "\\sum", "∏": "\\prod", "∫": "\\int", "∮": "\\oint", "⋃": "\\bigcup", "⋂": "\\bigcap"}
FUNCTIONS = ("arcsin arccos arctan sinh cosh tanh sin cos tan sec cosec csc cot log ln exp lim max min det "
             "gcd lcm hcf deg arg").split()
# KaTeX has no \lcm or \hcf: these are written \operatorname{lcm}, which it draws the same way
OPERATORNAME_ONLY = {"lcm", "hcf"}


def function_tex(word: str) -> str:
    return "\\operatorname{" + word + "}" if word in OPERATORNAME_ONLY else "\\" + word
RADICAL = "√"

# Computer Modern extension font (LaTeX's big delimiters, operators, radicals),
# indexed by character code as the PDF reports it.
CMEX = {}
for codes, ch in (((0x00, 0x10, 0x12, 0x20, 0x30, 0x40, 0x42), "("), ((0x01, 0x11, 0x13, 0x21, 0x31, 0x41, 0x43), ")"),
                  ((0x02, 0x14, 0x22, 0x32, 0x34, 0x36), "["), ((0x03, 0x15, 0x23, 0x33, 0x35, 0x37), "]"),
                  ((0x04, 0x16, 0x24), "⌊"), ((0x05, 0x17, 0x25), "⌋"), ((0x06, 0x18, 0x26), "⌈"),
                  ((0x07, 0x19, 0x27), "⌉"), ((0x08, 0x1A, 0x28, 0x38, 0x3A, 0x3C, 0x3E), "{"),
                  ((0x09, 0x1B, 0x29, 0x39, 0x3B, 0x3D), "}"), ((0x0A, 0x1C, 0x2A, 0x44), "⟨"),
                  ((0x0B, 0x1D, 0x2B, 0x45), "⟩"), ((0x0C, 0x3F), "|"), ((0x0D, 0x77), "‖"),
                  ((0x50, 0x58), "∑"), ((0x51, 0x59), "∏"), ((0x52, 0x5A), "∫"), ((0x48, 0x49), "∮"),
                  ((0x53, 0x5B), "⋃"), ((0x54, 0x5C), "⋂"), ((0x70, 0x71, 0x72, 0x73, 0x74), RADICAL)):
    for c in codes:
        CMEX[chr(c)] = ch

MATH_FONT_RE = re.compile(r"(CMMI|CMSY|CMEX|MSAM|MSBM|EUFM|EUSM|RSFS|Math|Symbol|STIX|KaTeX_(?!Main)|MT[- ]?Extra|"
                          r"MTSY|MTEX|MTMI|Euclid|rtxmi|txmi|txsy|txex|pxmi|pxsy|pxex|LMMath|NewCMMath|esint|Cambria.?Math)",
                          re.I)
DRAWING_FONT_RE = re.compile(r"^(LINE|LCIRCLE|LCIRCLEW)\d*", re.I)
BOLD_RE = re.compile(r"(bold|black|heavy|CMBX|CMB\d|semibold|demi)", re.I)
ITALIC_RE = re.compile(r"(italic|oblique|CMTI|CMMI|-It\b|It$)", re.I)


# --------------------------------------------------------------------------- items
@dataclass(eq=False)
class Item:
    x0: float
    y0: float
    x1: float
    y1: float
    base: float  # baseline y
    size: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def glyphs(self) -> list["Glyph"]:
        return []


@dataclass(eq=False)
class Glyph(Item):
    ch: str  # normalised character
    raw: str
    font: str
    bold: bool
    italic: bool
    math_font: bool
    page: int = 0
    known: bool = True
    ocr: bool = False  # read from a picture by OCR
    big: bool = False  # a tall bracket (\\left / \\right)
    space_before: bool = False  # the PDF has a space character just before this one

    def glyphs(self) -> list["Glyph"]:
        return [self]


@dataclass(eq=False)
class Frac(Item):
    num: list[Item] = field(default_factory=list)
    den: list[Item] = field(default_factory=list)

    def glyphs(self):
        return [g for it in self.num + self.den for g in it.glyphs()]


@dataclass(eq=False)
class Root(Item):
    body: list[Item] = field(default_factory=list)
    index: list[Item] = field(default_factory=list)
    sign: Glyph | None = None

    def glyphs(self):
        return ([self.sign] if self.sign else []) + [g for it in self.body + self.index for g in it.glyphs()]


@dataclass(eq=False)
class Over(Item):
    body: list[Item] = field(default_factory=list)

    def glyphs(self):
        return [g for it in self.body for g in it.glyphs()]


@dataclass(eq=False)
class BigOp(Item):
    op: Glyph | None = None
    upper: list[Item] = field(default_factory=list)
    lower: list[Item] = field(default_factory=list)

    def glyphs(self):
        return [self.op] + [g for it in self.upper + self.lower for g in it.glyphs()]


@dataclass
class Rule:
    x0: float
    x1: float
    y: float
    used: bool = False

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def union_box(items: list[Item]) -> tuple[float, float, float, float]:
    return (min(i.x0 for i in items), min(i.y0 for i in items), max(i.x1 for i in items), max(i.y1 for i in items))


# --------------------------------------------------------------------------- glyph extraction
def normalise_char(c: str, font: str) -> tuple[str, bool]:
    """Map a PDF character to a Unicode character; returns (char, known)."""
    if "CMEX" in font.upper() or re.search(r"Size[1-4]|KaTeX_Size", font):
        if c in CMEX:
            return CMEX[c], True
    if c in ("\u2212",):
        return "−", True
    if c in UNI_SUP or c in UNI_SUB:
        return c, True  # kept so the layout can make it a script
    n = unicodedata.normalize("NFKC", c)
    if len(n) == 1:
        c = n
    elif n in ("fi", "fl", "ff", "ffi", "ffl"):
        return n, True
    cat = unicodedata.category(c) if len(c) == 1 else "Lo"
    if cat in ("Cc", "Co", "Cs", "Cn") or c == "\ufffd":
        return c, False
    return c, True


# TeX font metrics (height, depth in em) for glyphs that hang below the baseline.
# PDF text extraction only knows font-wide ascent/descent, which is badly wrong
# for these, so the real extent is restored from the fonts' published metrics.
_CMEX_DEPTH = {}
for _codes, _depth in (((0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0E, 0x0F), 1.16),
                       ((0x0C, 0x0D), 0.6), ((0x10, 0x11, 0x1C, 0x1D, 0x1E, 0x1F), 1.76),
                       ((0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B), 2.36),
                       ((0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D), 2.96),
                       ((0x50, 0x51, 0x53, 0x54), 1.0), ((0x52, 0x48), 1.11), ((0x58, 0x59, 0x5B, 0x5C), 1.4),
                       ((0x5A, 0x49), 2.22), ((0x70,), 1.2), ((0x71,), 1.8), ((0x72,), 2.4), ((0x73,), 3.0),
                       ((0x74,), 1.8)):
    for _c in _codes:
        _CMEX_DEPTH[chr(_c)] = _depth


def _vertical_extent(raw: str, norm: str, font: str, base: float, size: float,
                     y0: float, y1: float) -> tuple[float, float]:
    f = font.upper()
    if "CMEX" in f and raw in _CMEX_DEPTH:
        return base - 0.04 * size, base + _CMEX_DEPTH[raw] * size
    if norm == RADICAL and ("CMSY" in f or "CMEX" in f):
        return base - 0.04 * size, base + 0.96 * size
    return y0, y1


_STRETCHY = set("()[]{}|‖⌊⌋⌈⌉⟨⟩") | {RADICAL} | set(BIG_OPS)


def _ink_extent(dl, page_rect, x0: float, x1: float, y0: float, y1: float, size: float):
    """Real vertical extent of a glyph, measured from the rendered page.

    Needed for roots, big brackets and big operators in OpenType maths fonts,
    whose reported boxes are those of the normal-size glyph.
    """
    import pymupdf

    zoom = 4
    clip = pymupdf.Rect(x0 + 0.25, max(page_rect.y0, y0 - 1.5 * size), x1 - 0.25,
                        min(page_rect.y1, y1 + 1.5 * size))
    if clip.width <= 0.2 or clip.height <= 0.2:
        return None
    pix = dl.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    w, h, samples = pix.width, pix.height, pix.samples
    stride = pix.stride
    ink = [min(samples[r * stride: r * stride + w]) < 140 for r in range(h)]
    runs, start, gap = [], None, 0
    for r, on in enumerate(ink + [False] * 4):
        if on:
            if start is None:
                start = r
            gap = 0
            end = r
        elif start is not None:
            gap += 1
            if gap > 3:
                runs.append((start, end))
                start = None
    if not runs:
        return None
    mid = ((y0 + y1) / 2 - clip.y0) * zoom
    best = min(runs, key=lambda ab: 0 if ab[0] <= mid <= ab[1] else min(abs(ab[0] - mid), abs(ab[1] - mid)))
    return clip.y0 + best[0] / zoom, clip.y0 + (best[1] + 1) / zoom


def page_glyphs(page, page_no: int, raw: dict | None = None) -> list[Glyph]:
    """All visible characters on a PyMuPDF page (spaces dropped, drawing fonts skipped).

    *raw* is the page's rawdict when it was assembled elsewhere (e.g. with OCR text).
    """
    out: list[Glyph] = []
    spaced = False
    dl = None
    raw = raw if raw is not None else page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                font = span.get("font", "")
                if DRAWING_FONT_RE.match(font):
                    continue
                size = float(span.get("size", 0))
                flags = int(span.get("flags", 0))
                ocr = "GlyphLess" in font
                bold = not ocr and (bool(flags & 16) or bool(BOLD_RE.search(font)))
                italic = not ocr and (bool(flags & 2) or bool(ITALIC_RE.search(font)))
                math_font = not ocr and bool(MATH_FONT_RE.search(font))
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if c.isspace():
                        spaced = True
                    if not c or c.isspace() or c in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
                        continue
                    norm, known = normalise_char(c, font)
                    x0, y0, x1, y1 = ch["bbox"]
                    if x1 - x0 <= 0.01 and y1 - y0 <= 0.01:
                        continue
                    base = float(ch["origin"][1])
                    cmex = "CMEX" in font.upper()
                    y0, y1 = _vertical_extent(c, norm, font, base, size, y0, y1)
                    if "OpenSymbol" in font and norm in ("¿", "∨") and y1 - y0 > 1.15 * size:
                        norm = "|"  # LibreOffice's stretched absolute-value bars extract as these
                    if norm in _STRETCHY and math_font and not cmex and "CMSY" not in font.upper():
                        dl = dl or page.get_displaylist()
                        ext = _ink_extent(dl, page.rect, x0, x1, y0, y1, size)
                        if ext:
                            y0, y1 = ext
                            if norm != RADICAL and y1 - y0 > 1.3 * size:
                                base = (y0 + y1) / 2 + 0.25 * size
                    if cmex and c in _CMEX_DEPTH and norm != RADICAL:
                        base = (y0 + y1) / 2 + 0.25 * size  # big symbols sit centred on the maths axis
                    out.append(Glyph(x0, y0, x1, y1, base, size, norm, c, font, bold, italic,
                                     math_font, page_no, known, ocr, space_before=spaced))
                    spaced = False
    return out


_DASHES = set("—–-_─‒―")


def ocr_dash_rules(glyphs: list[Glyph]) -> tuple[list[Rule], list[Glyph]]:
    """OCR reads fraction bars as runs of dashes; turn wide ones back into bars."""
    if not any(g.ocr for g in glyphs):
        return [], glyphs
    S = body_size(glyphs)
    rules: list[Rule] = []
    drop: set[int] = set()
    dashes = sorted((g for g in glyphs if g.ocr and g.ch in _DASHES), key=lambda g: (round(g.cy), g.x0))
    run: list[Glyph] = []

    def flush():
        if run and run[-1].x1 - run[0].x0 >= 1.2 * S:
            rules.append(Rule(run[0].x0, run[-1].x1, sum(g.cy for g in run) / len(run)))
            drop.update(id(g) for g in run)

    for g in dashes:
        if run and (abs(g.cy - run[-1].cy) > 2 or g.x0 - run[-1].x1 > 1.5):
            flush()
            run = []
        run.append(g)
    flush()
    return rules, [g for g in glyphs if id(g) not in drop]


def page_radicals(page) -> tuple[list[Rule], list[Glyph]]:
    """Root signs drawn as lines (MathType, some word processors): a check mark plus a bar.

    Returned as a bar (Rule) and a synthetic "√" glyph, the same shape TeX produces.
    """
    rules: list[Rule] = []
    signs: list[Glyph] = []
    for d in page.get_drawings():
        segs = [(it[1], it[2]) for it in d.get("items", []) if it[0] == "l"]
        if len(segs) < 2 or len(segs) != len(d.get("items", [])):
            continue
        r = d["rect"]
        if r.height < 4 or r.width < 4:
            continue
        horiz = [(a, b) for a, b in segs if abs(a.y - b.y) < 0.4 and abs(a.x - b.x) > 2]
        if not horiz:
            continue
        top = min(horiz, key=lambda ab: ab[0].y)
        hx0, hx1 = min(top[0].x, top[1].x), max(top[0].x, top[1].x)
        others = [(a, b) for a, b in segs if (a, b) != top]
        if abs(top[0].y - r.y0) > 1 or not others or not all(max(a.x, b.x) <= hx0 + 1 for a, b in others):
            continue
        if not any(max(a.y, b.y) > r.y1 - 1 for a, b in others):
            continue  # the check mark must reach down to the bottom of the sign
        size = r.height
        rules.append(Rule(hx0, hx1, top[0].y))
        signs.append(Glyph(r.x0, r.y0, hx0, r.y1, r.y1 - 0.2 * size, size * 0.8, RADICAL, RADICAL, "drawn-radical",
                           False, False, True))
    return rules, signs


def page_rules(page) -> list[Rule]:
    """Thin horizontal lines/rects: fraction bars, root bars, overlines."""
    rules: list[Rule] = []
    for d in page.get_drawings():
        r = d["rect"]
        items = d.get("items", [])
        if r.height <= 1.6 and r.width >= 2.0:
            if all(it[0] in ("l", "re", "qu") for it in items):
                rules.append(Rule(float(r.x0), float(r.x1), float((r.y0 + r.y1) / 2)))
    return rules


# --------------------------------------------------------------------------- structure building
def body_size(glyphs: list[Glyph]) -> float:
    sizes = [round(g.size, 1) for g in glyphs if g.ch.isalnum()] or [round(g.size, 1) for g in glyphs]
    if not sizes:
        return 10.0
    return statistics.mode(sizes)


class Builder:
    """Builds nested structures (fractions, roots, limits) from glyphs and rules."""

    def __init__(self, glyphs: list[Glyph], rules: list[Rule], problems: list[str]):
        self.items: list[Item] = list(glyphs)
        self.rules = rules
        self.S = body_size(glyphs)
        self.problems = problems

    def _in_x(self, it: Item, x0: float, x1: float, tol: float = 1.0) -> bool:
        return x0 - tol <= it.cx <= x1 + tol

    def _same_line_outside(self, it: Item, x0: float, x1: float) -> bool:
        """True if *it* belongs to a line of text running on past the ends of [x0, x1].

        A line of prose above/below a fraction has characters right next to
        the bar's ends on the same baseline; a numerator does not.
        """
        tol = 0.2 * max(it.size, 1)
        reach = 1.6 * max(it.size, self.S * 0.6)
        for o in self.items:
            if o is it or not isinstance(o, Glyph):
                continue
            if any(r.x0 - 1 <= o.cx <= r.x1 + 1 and (r.x0, r.x1) != (x0, x1) and abs(o.cy - r.y) < 1.2 * self.S
                   for r in self.rules):
                continue  # the numerator/denominator of a neighbouring fraction, e.g. (2/5, 19/5)
            if abs(o.base - it.base) < tol and abs(o.size - it.size) < 0.5:
                if x0 - reach <= o.cx < x0 - 1.0 or x1 + 1.0 < o.cx <= x1 + reach:
                    return True
        return False

    def _collect(self, rule: Rule, above: bool, limit: float | None = None) -> list[Item]:
        S = self.S
        cands = [it for it in self.items if self._in_x(it, rule.x0, rule.x1)]
        if above:
            seeds = [it for it in cands if rule.y - 0.75 * max(it.size, S * 0.6) <= it.y1 <= rule.y + 0.9]
        else:
            seeds = [it for it in cands if rule.y - 0.9 <= it.y0 <= rule.y + 0.75 * max(it.size, S * 0.6)]
        seeds = [it for it in seeds if not self._same_line_outside(it, rule.x0, rule.x1)]
        if limit is not None:
            seeds = [it for it in seeds if it.y1 <= limit + 0.5]
        if not seeds:
            return []
        group = list(seeds)
        grew = True
        while grew:
            grew = False
            gy0, gy1 = min(i.y0 for i in group), max(i.y1 for i in group)
            bases = [i.base for i in group]
            ref = max(i.size for i in group)
            for it in cands:
                if it in group:
                    continue
                if above and it.y1 > rule.y + 0.9 or not above and it.y0 < rule.y - 0.9:
                    continue
                if limit is not None and it.y1 > limit + 0.5:
                    continue
                overlaps = it.y1 > gy0 - 0.1 * ref and it.y0 < gy1 + 0.1 * ref
                small = it.size < 0.86 * ref
                same_base = any(abs(it.base - b) < 0.3 * ref for b in bases)
                within = rule.x0 - 0.5 <= it.x0 and it.x1 <= rule.x1 + 0.5  # e.g. the 2 of tan^2 x
                if overlaps and (small or same_base) and (small and within
                                                          or not self._same_line_outside(it, rule.x0, rule.x1)):
                    group.append(it)
                    grew = True
        return group

    def _is_underline(self, rule: Rule) -> bool:
        """A line just under the baseline of body-size text that carries on past the line's ends."""
        S = self.S
        over = [it for it in self.items if isinstance(it, Glyph) and self._in_x(it, rule.x0, rule.x1)
                and 0 <= rule.y - it.base < 0.25 * S and abs(it.size - S) < 0.15 * S]
        if not over:
            return False
        base = statistics.median(g.base for g in over)
        for it in self.items:
            if isinstance(it, Glyph) and abs(it.base - base) < 0.1 * S and abs(it.size - S) < 0.15 * S \
                    and (0 <= rule.x0 - it.x1 < 1.5 * S or 0 <= it.x0 - rule.x1 < 1.5 * S):
                return True
        return False

    def _replace(self, old: list[Item], new: Item) -> None:
        ids = {id(o) for o in old}
        self.items = [i for i in self.items if id(i) not in ids]
        self.items.append(new)

    def build(self) -> list[Item]:
        for it in self.items:  # brackets drawn at a larger size than the text around them
            if isinstance(it, Glyph) and it.ch in "()[]|" and it.y1 - it.y0 > 1.45 * self.S:
                it.big = True
        self._big_ops()
        for rule in sorted(self.rules, key=lambda r: r.width):
            self._apply_rule(rule)
        self._orphan_radicals()
        return self.items

    def _apply_rule(self, rule: Rule) -> None:
        S = self.S
        # radical sign whose top meets the left end of the bar
        sign = None
        for it in self.items:
            if isinstance(it, Glyph) and it.ch == RADICAL and abs(it.x1 - rule.x0) < 0.35 * max(it.size, S) \
                    and abs(it.y0 - rule.y) < 0.45 * max(it.size, S):
                sign = it
                break
        if sign is not None:
            body = [it for it in self.items if it is not sign and self._in_x(it, rule.x0, rule.x1, 0.5)
                    and it.y0 >= rule.y - 0.6 and it.y1 <= sign.y1 + 0.35 * S]
            h = sign.y1 - sign.y0
            index = [it for it in self.items if isinstance(it, Glyph) and it is not sign and it.size < 0.86 * S
                     and sign.x0 - 0.6 * S <= it.x0 and it.x1 <= sign.x0 + 0.75 * (sign.x1 - sign.x0)
                     and sign.y0 - 0.5 * S <= it.base <= sign.y0 + 0.55 * h]
            if not body:
                return
            x0, y0, x1, y1 = union_box(body + [sign] + index)
            base = statistics.median([b.base for b in body])
            rule.used = True
            self._replace(body + [sign] + index, Root(x0, min(y0, rule.y), x1, y1, base, max(b.size for b in body),
                                                      body=body, index=index, sign=sign))
            return
        if self._is_underline(rule):
            rule.used = True  # emphasis, not maths
            return
        num = self._collect(rule, above=True)
        den = self._collect(rule, above=False)
        if num and den:
            rule.used = True
            size = max(i.size for i in num + den)
            x0, y0, x1, y1 = union_box(num + den)
            axis_to_base = 0.25 * max(size, S)
            frac = Frac(min(x0, rule.x0), y0, max(x1, rule.x1), y1, rule.y + axis_to_base, size, num=num, den=den)
            self._replace(num + den, frac)
        elif den and not num:
            tight = [it for it in den if it.y0 - rule.y < 0.5 * S]
            if tight and rule.width < 12 * S:
                rule.used = True
                x0, y0, x1, y1 = union_box(den)
                base = statistics.median([b.base for b in den])
                self._replace(den, Over(x0, rule.y, x1, y1, base, max(i.size for i in den), body=den))
                self.problems.append("an overline was interpreted as \\overline{...}")

    def _big_ops(self) -> None:
        S = self.S
        for op in [it for it in self.items if isinstance(it, Glyph) and it.ch in BIG_OPS]:
            if op not in self.items:
                continue
            h = op.y1 - op.y0
            small = lambda it: it.size < 0.86 * max(op.size, S)  # noqa: E731
            upper = [it for it in self.items if it is not op and self._in_x(it, op.x0, op.x1, 2) and small(it)
                     and it.cy < op.cy - 0.25 * h and it.y1 >= op.y0 - 1.1 * S]
            lower = [it for it in self.items if it is not op and self._in_x(it, op.x0, op.x1, 2) and small(it)
                     and it.cy > op.cy + 0.25 * h and it.y0 <= op.y1 + 1.1 * S]
            # integral limits set to the right are scripts, handled by the line layout: a limit that
            # carries on past the sign's right edge ("−" of "−√2") is not a limit under the sign
            def runs_right(group: list[Item]) -> bool:
                return any(o not in group and o is not op and o.x0 > op.x1 - 0.5 and small(o)
                           and any(abs(o.base - g.base) < 0.2 * S and 0 <= o.x0 - g.x1 < 0.3 * S for g in group)
                           for o in self.items)
            if upper and runs_right(upper):
                upper = []
            if lower and runs_right(lower):
                lower = []
            # a big (display) operator is taller than the text around it; centre it on the maths axis
            base = op.cy + 0.25 * S if op.y1 - op.y0 > 1.2 * S else op.base
            items = [op] + upper + lower
            x0, y0, x1, y1 = union_box(items)
            self._replace(items, BigOp(x0, y0, x1, y1, base, op.size, op=op, upper=upper, lower=lower))

    def _orphan_radicals(self) -> None:
        for it in list(self.items):
            if isinstance(it, Glyph) and it.ch == RADICAL:
                self.problems.append("a square-root sign without a bar over its contents - check the root")


# --------------------------------------------------------------------------- horizontal layout
@dataclass(eq=False)
class Node:
    item: Item
    sup: list["Node"] = field(default_factory=list)
    sub: list["Node"] = field(default_factory=list)


def is_tall(it: Item) -> bool:
    return isinstance(it, (Frac, BigOp)) or (isinstance(it, Glyph) and it.y1 - it.y0 > 1.6 * it.size)


def hlist(items: list[Item], problems: list[str]) -> list[Node]:
    """Lay out items on one line: attach super/subscripts to their bases."""
    if not items:
        return []
    items = sorted(items, key=lambda i: (i.x0, i.y0))
    sizes = [i.size for i in items if isinstance(i, Glyph) and not is_tall(i)] or [i.size for i in items]
    base_size = max(sizes)
    main = [i.base for i in items if isinstance(i, Glyph) and i.size >= 0.86 * base_size and not is_tall(i)] or \
           [i.base for i in items]
    baseline = statistics.median(main)
    nodes: list[Node] = []
    i = 0
    while i < len(items):
        it = items[i]
        if nodes and isinstance(it, Glyph) and (it.ch in UNI_SUP or it.ch in UNI_SUB):
            # typed as a superscript/subscript character (n², x₁): a script, not a new symbol
            table = UNI_SUP if it.ch in UNI_SUP else UNI_SUB
            j = i
            while j < len(items) and isinstance(items[j], Glyph) and items[j].ch in table:
                j += 1
            run = [replace(g, ch=table[g.ch]) for g in items[i:j]]
            target = nodes[-1]
            (target.sup if table is UNI_SUP else target.sub).extend(Node(g) for g in run)
            i = j
            continue
        small = it.size < 0.86 * base_size
        direction = None
        if small and nodes:
            if it.base < baseline - 0.18 * base_size:
                direction = "sup"
            elif it.base > baseline + 0.08 * base_size:
                direction = "sub"
        if direction is None:
            nodes.append(Node(it))
            i += 1
            continue
        # collect a run of script items in this direction; the other script of the same base
        # (a limit above while collecting the one below) may sit in between
        run = [it]
        other: list[Item] = []
        j = i + 1
        while j < len(items):
            nx = items[j]
            if nx.size >= 0.86 * base_size:
                break
            nd = "sup" if nx.base < baseline - 0.18 * base_size else "sub" if nx.base > baseline + 0.08 * base_size \
                else None
            if nd is not None and nd != direction:
                other.append(nx)
                j += 1
                continue
            if nd != direction or nx.x0 - run[-1].x1 > 0.45 * base_size:
                break
            run.append(nx)
            j += 1
        if other:  # put the skipped items back so they are handled next
            rest = [x for x in items[i + 1:j] if x in other]
            items = items[:i + 1] + rest + [x for x in items[i + 1:j] if x not in other and x not in run] + items[j:]
            j = i + 1
            items = items[:i] + [x for x in items[i:] if x not in run]
            j = i
        target = nodes[-1]
        slot = target.sup if direction == "sup" else target.sub
        if slot:
            problems.append("a character has two superscripts/subscripts - check the exponents")
        slot.extend(hlist(run, problems))
        i = j
    return nodes


# --------------------------------------------------------------------------- LaTeX generation
_CTRL_END = re.compile(r"\\[A-Za-z]+$")


def _join(parts: list[tuple[str, bool]]) -> str:
    """Join LaTeX parts; bool = source had a visible gap before this part."""
    out = ""
    for tex, gap in parts:
        if not tex:
            continue
        if out and (gap or (_CTRL_END.search(out) and re.match(r"[A-Za-z0-9]", tex))):
            out += " "
        out += tex
    return out


def _wrap(tex: str) -> str:
    if len(tex) == 1 or re.fullmatch(r"\\[A-Za-z]+", tex):
        return tex
    return "{" + tex + "}"


def glyph_tex(g: Glyph, problems: list[str]) -> str:
    c = g.ch
    if not g.known:
        problems.append(f"unrecognised character U+{ord(g.raw[0]):04X} from font {g.font}")
        return "?"
    if c in GREEK:
        return GREEK[c]
    if c in SYMBOLS:
        return SYMBOLS[c]
    if c == "°":
        return "^\\circ"
    if c == "\\":
        return "\\backslash"
    if c in "^_~":
        return "\\" + c + "{}"
    return c


def node_right(node: Node) -> float:
    right = node.item.x1
    for n in node.sup + node.sub:
        right = max(right, node_right(n))
    return right


_RELATIONS = {"=", "<", ">", "\\le", "\\ge", "\\ne", "\\leqslant", "\\geqslant", "\\approx", "\\equiv",
              "\\sim", "\\simeq", "\\cong", "\\propto", "\\to", "\\Rightarrow", "\\Leftarrow",
              "\\Leftrightarrow", "\\leftrightarrow", "\\Longrightarrow", "\\in", "\\notin", "\\subset",
              "\\subseteq", "\\supset", "\\supseteq", "\\ll", "\\gg", "\\leftarrow"}
_BINOPS = {"+", "-", "\\times", "\\div", "\\pm", "\\mp", "\\cdot", "\\cup", "\\cap"}
_TOKEN_RE = re.compile(r"\\[A-Za-z]+|\\.|\S")


def tidy_math(tex: str) -> str:
    """Consistent spacing: around relations and binary operators and after commas,
    none inside super/subscripts; spaces the source had between terms are kept."""
    toks = [(m.group(0), m.start()) for m in _TOKEN_RE.finditer(tex)]
    out: list[str] = []
    prev = None
    prev_end = 0
    was_unary = False
    stack: list[bool] = []  # per open brace: is it a script group?
    for t, start in toks:
        in_script = any(stack)
        had_space = start > prev_end and prev is not None
        unary = t in ("+", "-") and (prev is None or prev in _RELATIONS or prev in _BINOPS or
                                     prev in ("(", "[", "{", "^", "_", ",", "\\left(", "\\left[", "\\left|"))
        spaced = (t in _RELATIONS or t in _BINOPS) and not unary
        want = False
        if out and not in_script:
            if spaced or prev in _RELATIONS or (prev in _BINOPS and not was_unary) or prev == ",":
                want = True
            elif had_space and prev not in ("(", "[", "{", "^", "_", "\\left(", "\\left[") and \
                    t not in (")", "]", "}", ",", "^", "_", "\\right)", "\\right]"):
                want = True
        if out and not want and re.fullmatch(r"\\[A-Za-z]+", prev or "") and re.match(r"[A-Za-z0-9]", t):
            want = True
        if want and not out[-1].endswith(" "):
            out.append(" ")
        out.append(t)
        if t == "{":
            stack.append(prev in ("^", "_"))
        elif t == "}" and stack:
            stack.pop()
        was_unary = unary
        prev = t
        prev_end = start + len(t)
    return clean_braces("".join(out).strip())


def clean_braces(tex: str) -> str:
    """Drop braces that do nothing: empty scripts x^{}, empty groups {}, doubled {{...}},
    and single-character scripts x^{2} -> x^2."""
    prev = None
    while prev != tex:
        prev = tex
        tex = re.sub(r"[\^_]\{\s*\}", "", tex)
        tex = re.sub(r"(?<![\\A-Za-z}\]])\{\s*\}", "", tex)
        tex = re.sub(r"\{\{([^{}]*)\}\}", r"{\1}", tex)
        tex = re.sub(r"([\^_])\{([A-Za-z0-9])\}", r"\1\2", tex)
    return tex


def math_tex(nodes: list[Node], problems: list[str]) -> str:
    """LaTeX for a list of nodes inside maths."""
    parts: list[tuple[str, bool]] = []
    prev_node: Node | None = None
    idx = 0
    while idx < len(nodes):
        node = nodes[idx]
        it = node.item
        gap = prev_node is not None and it.x0 - node_right(prev_node) > 0.17 * max(it.size, prev_node.item.size)
        # function names written in upright letters: sin, cos, log, ...
        word, consumed = _function_word(nodes, idx)
        if word:
            tex = function_tex(word)
            last = nodes[idx + consumed - 1]
            tex += _scripts(last, problems)
            parts.append((tex, gap))
            prev_node = last
            idx += consumed
            continue
        tex = _node_core(node, problems) + _scripts(node, problems)
        if isinstance(prev_node, Node) and isinstance(prev_node.item, BigOp):
            gap = True  # \int_0^a 6x, not \int_0^a6x
        nxt = nodes[idx + 1].item if idx + 1 < len(nodes) else None
        if isinstance(it, Glyph) and it.ch == "d" and it.math_font and not _math_italic_char(it) \
                and isinstance(nxt, Glyph) and _math_italic_char(nxt) and prev_node is not None:
            tex, gap = "\\,d", False  # Word's upright differential d: "12\,dx"
        parts.append((tex, gap))
        prev_node = node
        idx += 1
    return _join(parts)


def _function_word(nodes: list[Node], i: int) -> tuple[str | None, int]:
    letters = ""
    j = i
    while j < len(nodes):
        it = nodes[j].item
        if not (isinstance(it, Glyph) and it.ch.isalpha() and it.ch.isascii() and not (it.italic and it.math_font)):
            break
        if _math_italic_char(it):
            break  # Word writes variables as maths-italic letters (𝑥) and sin/cos upright: "sin𝑥"
        if j > i and it.x0 - nodes[j - 1].item.x1 > 0.17 * it.size:
            break
        if j > i and (nodes[j - 1].sup or nodes[j - 1].sub):
            break
        letters += it.ch
        j += 1
    if letters in FUNCTIONS:
        if i > 0:
            p = nodes[i - 1].item
            if isinstance(p, Glyph) and p.ch.isalpha() and nodes[i].item.x0 - p.x1 < 0.17 * p.size \
                    and not getattr(nodes[i].item, "space_before", False):
                return None, 0
        return letters, len(letters)
    return None, 0


def _node_core(node: Node, problems: list[str]) -> str:
    it = node.item
    if isinstance(it, Glyph):
        if it.ch in BIG_OPS:
            return BIG_OPS[it.ch]
        tex = glyph_tex(it, problems)
        if it.ch in "()[]|" and (it.big or it.y1 - it.y0 > 1.7 * it.size):
            return "\x00big" + it.ch
        return tex
    if isinstance(it, Frac):
        return "\\frac{" + _pair_big_delimiters(math_tex(hlist(it.num, problems), problems)) + "}{" + \
            _pair_big_delimiters(math_tex(hlist(it.den, problems), problems)) + "}"
    if isinstance(it, Root):
        tex = "\\sqrt"
        if it.index:
            tex += "[" + math_tex(hlist(it.index, problems), problems) + "]"
        return tex + "{" + math_tex(hlist(it.body, problems), problems) + "}"
    if isinstance(it, Over):
        return "\\overline{" + math_tex(hlist(it.body, problems), problems) + "}"
    if isinstance(it, BigOp):
        tex = BIG_OPS.get(it.op.ch, "?")
        if it.lower:
            tex += "_" + _wrap(math_tex(hlist(it.lower, problems), problems))
        if it.upper:
            tex += "^" + _wrap(math_tex(hlist(it.upper, problems), problems))
        return tex
    return "?"


def _scripts(node: Node, problems: list[str]) -> str:
    tex = ""
    if node.sub:
        tex += "_" + _wrap(math_tex(node.sub, problems))
    if node.sup:
        sup = math_tex(node.sup, problems)
        if sup in ("\\circ", "^\\circ"):
            tex += "^\\circ"
        elif re.fullmatch(r"'+", sup):
            tex += sup
        else:
            tex += "^" + _wrap(sup)
    return tex


def _pair_big_delimiters(tex: str) -> str:
    """Big ( ) become \\left( \\right) when both are present, plain otherwise."""
    opens = tex.count("\x00big(") + tex.count("\x00big[")
    closes = tex.count("\x00big)") + tex.count("\x00big]")
    bars = tex.count("\x00big|")
    if opens and opens == closes:
        tex = tex.replace("\x00big(", "\\left(").replace("\x00big[", "\\left[")
        tex = tex.replace("\x00big)", "\\right)").replace("\x00big]", "\\right]")
    if bars and bars % 2 == 0:
        k = 0
        while "\x00big|" in tex:
            tex = tex.replace("\x00big|", "\\left|" if k % 2 == 0 else "\\right|", 1)
            k += 1
    return re.sub("\x00big(.)", r"\1", tex)


# --------------------------------------------------------------------------- text vs maths
@dataclass
class Token:
    kind: str  # "text" | "math" | "neutral"
    tex: str
    gap: bool  # visible gap before this token
    node: Node | None = None


def classify_node(node: Node) -> str:
    it = node.item
    if node.sup or node.sub or not isinstance(it, Glyph):
        return "math"
    c = it.ch
    if c in GREEK or c in SYMBOLS or c in PLAIN_MATH or c in BIG_OPS or c == RADICAL or c == "°":
        return "neutral" if c in ("'",) else "math"
    if c.isdigit():
        return "neutral"
    if c.isalpha():
        if it.math_font:
            return "math"
        return "text"
    if c in PUNCT or c == "-":
        return "neutral"
    return "math" if not c.isascii() else "neutral"


def line_tokens(nodes: list[Node], problems: list[str]) -> list[Token]:
    """Group a laid-out line into prose words and maths nodes."""
    tokens: list[Token] = []
    i = 0
    while i < len(nodes):
        node = nodes[i]
        it = node.item
        prev_n = nodes[i - 1] if i else None
        gap = prev_n is not None and (it.x0 - node_right(prev_n) > 0.17 * max(it.size, prev_n.item.size)
                                      or getattr(it, "space_before", False) and it.math_font)
        kind = classify_node(node)
        words = _maths_font_prose(nodes, i)
        if words:  # a sentence typed inside an equation: "You may use the fact that"
            for k, (word, a, b) in enumerate(words):
                wgap = gap if k == 0 else True
                tokens.append(Token("text", word, wgap, nodes[a]))
            i = words[-1][2]
            continue
        if kind == "math" and isinstance(it, Glyph) and it.ch.isalpha():
            word, consumed = _function_word(nodes, i)
            if word:  # sin, cos, log set in a maths font (Word's Cambria Math)
                last = nodes[i + consumed - 1]
                gap = gap or prev_n is not None and isinstance(prev_n.item, BigOp)
                tokens.append(Token("math", function_tex(word) + _scripts(last, problems), gap, node))
                i += consumed
                continue
        if kind == "text":
            # a whole word in a text font (letters, apostrophes, hyphens)
            j = i
            word_nodes = []
            while j < len(nodes):
                n = nodes[j]
                g = n.item
                if not isinstance(g, Glyph):
                    break
                if j > i and g.x0 - nodes[j - 1].item.x1 > 0.17 * g.size:
                    break
                if not (g.ch.isalpha() and not g.math_font or g.ch in "'’-" and j > i):
                    break
                word_nodes.append(n)
                j += 1
                if n.sup or n.sub:
                    break  # a script ends the word
            word = "".join(n.item.ch for n in word_nodes)
            scripted = bool(word_nodes[-1].sup or word_nodes[-1].sub)
            italic_var = _italic_maths_word(word, word_nodes, nodes, i, j)
            if word in FUNCTIONS or italic_var or (scripted and len(word) == 1):
                tokens.append(Token("math", math_tex(word_nodes, problems), gap, word_nodes[0]))
            elif scripted:  # a unit such as cm² or km²: the word stays prose, the power is maths
                tokens.append(Token("text", word, gap, word_nodes[0]))
                tokens.append(Token("math", _scripts(word_nodes[-1], problems), False, word_nodes[-1]))
            else:
                tokens.append(Token("text", word, gap, word_nodes[0]))
            i = j
            continue
        tex = math_tex([node], problems) if kind == "math" else glyph_tex(it, problems) if isinstance(it, Glyph) \
            else math_tex([node], problems)
        if isinstance(it, Glyph) and it.ch in "()[]|" and (it.big or it.y1 - it.y0 > 1.7 * it.size):
            tex = "\x00big" + it.ch
            kind = "math"
        if kind == "neutral" and isinstance(it, Glyph) and it.ch == "-" and not it.math_font:
            tex = "-"
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            if nxt is not None and isinstance(nxt.item, Glyph) and nxt.item.ch.isalpha() and not nxt.item.math_font \
                    and nxt.item.x0 - it.x1 < 0.17 * it.size:
                kind = "text"  # the hyphen of "y-axis"
        if prev_n is not None and isinstance(prev_n.item, BigOp):
            gap = True  # \int_0^a 6x, not \int_0^a6x
        nxt = nodes[i + 1].item if i + 1 < len(nodes) else None
        if isinstance(it, Glyph) and it.ch == "d" and it.math_font and not _math_italic_char(it) \
                and isinstance(nxt, Glyph) and _math_italic_char(nxt) and prev_n is not None:
            tex, gap, kind = "\\,d", False, "math"  # Word's upright differential d: "12\,dx"
        tokens.append(Token(kind, tex, gap, node))
        i += 1
    return _upright_variables(tokens)


_OPERATOR_CHARS = set("+-−=<>≤≥≠×÷^(")


def _upright_variables(tokens: list[Token]) -> list[Token]:
    """A lone upright letter (other than a, A, I) next to maths or an operator is a variable,
    as in "n² + n" typed in a word processor without an equation editor."""
    def mathy(t: Token | None, left: bool) -> bool:
        if t is None:
            return False
        if t.kind == "math":
            return True
        if t.kind != "neutral":
            return False
        return (t.tex[-1:] if left else t.tex[:1]) in _OPERATOR_CHARS - ({"("} if left else set())
    changed = True
    while changed:
        changed = False
        for k, t in enumerate(tokens):
            if t.kind == "text" and len(t.tex) == 1 and t.tex.isalpha() and t.tex not in "aAI":
                if mathy(tokens[k - 1] if k else None, True) or mathy(tokens[k + 1] if k + 1 < len(tokens) else None,
                                                                    False):
                    t.kind = "math"
                    changed = True
    return tokens


_ORDINAL_RE = re.compile(r"(\d+)\^\{?(st|nd|rd|th)\}?")
_ORDINAL_IN_MATHS_RE = re.compile(r"(?<=\d\^\{)(st|nd|rd|th)(?=\})")


def _split_islands(tokens: list[Token]) -> list[Token]:
    """Mark a top-level comma/semicolon followed by a space as prose, so
    "$a > 2$, $b < 3$" stays two expressions (commas inside brackets stay maths)."""
    depth = 0
    for k, t in enumerate(tokens):
        if t.kind == "text":
            depth = 0
            continue
        opens = t.tex.count("(") + t.tex.count("[")
        closes = t.tex.count(")") + t.tex.count("]")
        depth = max(0, depth + opens - closes)
        if t.kind == "neutral" and t.tex in (",", ";", ".") and depth == 0 and k + 1 < len(tokens) \
                and tokens[k + 1].gap and not (t.tex == "." and k + 1 < len(tokens) and tokens[k + 1].tex == "."):
            t.kind = "sep"
    return tokens


# Words a paper might set in italics for emphasis; anything else short and
# italic in a text font (AB, BAC, dx, PQRS) is maths set in the text italic,
# as fonts like Times (mathptmx) and many word processors do.
_EMPHASIS = set("""a an the is are be was of and or not no nor only all any none each every exactly at least most
if then true false never must can cannot both neither either one two three four five six more less than
same different all always sometimes does do this that these those it its as in on for to""".split())


def _italic_maths_word(word: str, word_nodes: list[Node], nodes: list[Node], i: int, j: int) -> bool:
    first = word_nodes[0].item
    if isinstance(first, Glyph) and first.ocr:
        # OCR has no fonts: a lone letter other than "a"/"A"/"I" is a variable
        return len(word) == 1 and word not in ("a", "A", "I")
    if not all(isinstance(n.item, Glyph) and n.item.italic and not n.item.bold for n in word_nodes):
        return False
    if len(word) == 1:
        return True
    if word.lower() in _EMPHASIS or not word.isalpha():
        return False
    if _italic_word_len(nodes, j, 1) >= 3 or _italic_word_len(nodes, i - 1, -1) >= 3:
        return False  # part of an italic sentence: "You should not ..."
    if len(word) <= 4:
        return True

    def mathy(n: Node) -> bool:
        return classify_node(n) == "math"

    before = nodes[i - 1] if i else None
    after = nodes[j] if j < len(nodes) else None
    touching = (before is not None and first.x0 - node_right(before) < 0.17 * first.size and mathy(before)) or \
        (after is not None and after.item.x0 - node_right(word_nodes[-1]) < 0.17 * first.size and mathy(after))
    return touching


def tokens_to_text(tokens: list[Token]) -> str:
    """Prose with maths islands wrapped in $...$."""
    tokens = _split_islands(tokens)
    for t in tokens:
        if t.kind == "sep":
            t.kind = "text"
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t.kind == "text":
            out.append((" " if t.gap and out else "") + t.tex)
            i += 1
            continue
        j = i
        while j < n and tokens[j].kind != "text":
            j += 1
        island = tokens[i:j]
        if not any(tok.kind == "math" for tok in island):
            text = ""
            for k, tok in enumerate(island):
                text += (" " if tok.gap and (k or out) else "") + (tok.node.item.ch if tok.node and isinstance(
                    tok.node.item, Glyph) else tok.tex)
            out.append(text)
            i = j
            continue
        # trailing punctuation that is not closing a bracket opened inside the maths stays outside
        tail: list[Token] = []
        while island and island[-1].kind == "neutral" and island[-1].tex in (",", ".", ";", ":", "?", "!", ")", "]", "(", "["):
            last = island[-1]
            body = "".join(tok.tex for tok in island[:-1])
            if last.tex in ")]" and body.count("(" if last.tex == ")" else "[") > body.count(last.tex):
                break
            tail.insert(0, island.pop())
        head: list[Token] = []
        # punctuation stuck to the word before ("Note:", "e.g.") is prose, not the start of the maths
        while island and island[0].kind == "neutral" and island[0].tex in (",", ".", ";", ":", "?", "!") \
                and not island[0].gap:
            head.append(island.pop(0))
        while island and island[0].kind == "neutral" and island[0].tex in ("(", "["):
            rest = "".join(tok.tex for tok in island[1:])
            close = ")" if island[0].tex == "(" else "]"
            if rest.count(close) > rest.count(island[0].tex):
                break
            head.append(island.pop(0))
        lead_gap = (head[0].gap if head else island[0].gap if island else False) and bool(out)
        prefix = "".join(tok.tex for tok in head)
        maths = tidy_math(_pair_big_delimiters(_join([(tok.tex, tok.gap and k > 0) for k, tok in enumerate(island)])))
        suffix = "".join((" " if tok.gap and tok.tex not in ")]" else "") + tok.tex for tok in tail)
        if head and island and island[0].gap:
            prefix += " "
        ordinal = _ORDINAL_RE.fullmatch(maths)
        if ordinal:  # "2nd", "3rd" with a raised ending is prose
            body = ordinal.group(1) + ordinal.group(2)
        else:
            body = "$" + _ORDINAL_IN_MATHS_RE.sub(r"\\text{\1}", maths) + "$" if maths else ""
        out.append((" " if lead_gap else "") + prefix + body + suffix)
        i = j
    return "".join(out).strip()


# --------------------------------------------------------------------------- lines and paragraphs
_STATEMENT_RE = re.compile(r"^(?:I{1,3}|IV|VI{0,3}|\((?:[a-h]|i{1,3}|iv|vi{0,3})\)|(?:[a-h]|i{1,3}|iv)\))(?=\s)")
@dataclass(eq=False)
class Line:
    items: list[Item]
    base: float
    x0: float
    x1: float
    y0: float
    y1: float
    page: int


def build_lines(items: list[Item], page_of: dict[int, int], S: float) -> list[Line]:
    """Cluster items into text lines (per page), scripts joining the line they belong to."""
    lines: list[Line] = []
    pages = sorted({page_of.get(id(i), 0) for i in items})
    for page in pages:
        its = [i for i in items if page_of.get(id(i), 0) == page]
        primary = [i for i in its if i.size >= 0.86 * S or not isinstance(i, Glyph) and not _script_sized(i, S)]
        secondary = [i for i in its if i not in primary]
        primary.sort(key=lambda i: i.base)
        groups: list[list[Item]] = []
        for it in primary:
            if groups and abs(it.base - statistics.median(g.base for g in groups[-1])) < 0.35 * S:
                groups[-1].append(it)
            else:
                groups.append([it])
        for it in secondary:
            best, best_d = None, None
            for g in groups:
                base = statistics.median(x.base for x in g)
                d = it.base - base
                near = min(abs(it.cx - x.cx) for x in g)
                ok = -0.95 * S <= d <= 0.6 * S
                if not ok:
                    # limits/scripts of a tall neighbour (integral, big bracket, fraction) sit further out
                    tall = [x for x in g if (x.y1 - x.y0) > 1.3 * S and (x.x0 - 1.5 * S) <= it.cx <= (x.x1 + 1.5 * S)]
                    ok = any(x.y0 - 0.5 * S <= it.cy <= x.y1 + 0.5 * S for x in tall)
                if ok:
                    score = abs(d) + near * 0.01
                    if best is None or score < best_d:
                        best, best_d = g, score
            if best is None:
                groups.append([it])
            else:
                best.append(it)
        for g in groups:
            x0, y0, x1, y1 = union_box(g)
            lines.append(Line(g, statistics.median(i.base for i in g), x0, x1, y0, y1, page))
    lines.sort(key=lambda ln: (ln.page, ln.base))
    return lines


def lines_to_text(lines: list[Line], problems: list[str], column: tuple[float, float] | None = None) -> str:
    """Paragraphs of text; lines that are only maths and set apart become $$...$$ display maths."""
    if not lines:
        return ""
    S = body_size([g for ln in lines for it in ln.items for g in it.glyphs()])
    col_x0 = column[0] if column else min(ln.x0 for ln in lines)
    col_x1 = column[1] if column else max(ln.x1 for ln in lines)
    rendered = []
    for ln in lines:
        nodes = hlist(ln.items, problems)
        toks = line_tokens(nodes, problems)
        only_math = toks and all(t.kind != "text" for t in toks) and any(t.kind == "math" for t in toks)
        centred = abs((ln.x0 + ln.x1) / 2 - (col_x0 + col_x1) / 2) < 0.12 * (col_x1 - col_x0)
        indented = ln.x0 - col_x0 > 2.5 * S
        display = bool(only_math and (indented or centred) and len(lines) > 1)
        if display:
            core = [t for t in toks]
            trail = ""
            while core and core[-1].kind == "neutral" and core[-1].tex in (",", ".", ";"):
                trail = core.pop().tex + trail
            tex = tidy_math(_pair_big_delimiters(_join([(t.tex, t.gap and k > 0) for k, t in enumerate(core)])))
            rendered.append((ln, f"$${tex}{trail}$$", True))
        else:
            rendered.append((ln, tokens_to_text(toks), False))
    gaps = [b[0].y0 - a[0].y1 for a, b in zip(rendered, rendered[1:]) if b[0].page == a[0].page
            and not a[2] and not b[2]]
    typical = statistics.median(gaps) if gaps else 0.3 * S
    paras: list[str] = []
    cur = ""
    para_start = ""
    prev = None
    for ln, text, display in rendered:
        if not text:
            continue
        new_para = prev is None or display or prev[2]
        if prev is not None and not new_para:
            gap = ln.y0 - prev[0].y1
            if ln.page != prev[0].page:
                gap = typical
            if gap > max(0.7 * S, typical + 0.45 * S) or ln.x0 > prev[0].x0 + 0.8 * S:
                new_para = True  # extra space, or an indented line (new paragraph / statement)
            if _STATEMENT_RE.match(text):
                new_para = True
            elif ln.x0 < prev[0].x0 - 0.8 * S and _STATEMENT_RE.match(para_start):
                new_para = True  # back to the margin after an indented statement
        if new_para:
            if cur:
                paras.append(cur)
            cur = text
            para_start = text
        else:
            cur = cur + ("" if cur.endswith("-") else " ") + text
        prev = (ln, text, display)
    if cur:
        paras.append(cur)
    return "\n\n".join(p.strip() for p in paras if p.strip())


def _script_sized(it: Item, S: float) -> bool:
    """A small root or fraction set as a limit or exponent (the limit sqrt 2 of an integral, x^{3/2})."""
    return isinstance(it, (Root, Frac)) and all(g.size < 0.86 * S for g in it.glyphs())


def _math_italic_char(g: Glyph) -> bool:
    """A Unicode maths-italic letter such as U+1D465 (italic x), as Word's equation editor writes variables."""
    raw = g.raw or ""
    return len(raw) == 1 and 0x1D400 <= ord(raw) <= 0x1D7FF and "ITALIC" in unicodedata.name(raw, "")


def _maths_font_prose(nodes: list[Node], i: int) -> list[tuple[str, int, int]]:
    """Words of a sentence set in a maths font, starting at nodes[i]: two or more letter-only words
    of two or more letters, one of them a real-looking word (3+ letters with a vowel).
    Returns (word, start, end) per word, or [] when this is maths."""
    words: list[tuple[str, int, int]] = []
    j = i
    while j < len(nodes):
        a = j
        word = ""
        while j < len(nodes):
            g = nodes[j].item
            if not (isinstance(g, Glyph) and g.math_font and g.ch.isalpha() and g.ch.isascii()) \
                    or nodes[j].sup or nodes[j].sub:
                break
            if j > a and (g.x0 - nodes[j - 1].item.x1 > 0.17 * g.size or g.space_before):
                break
            word += g.ch
            j += 1
        if len(word) < 2 or word in FUNCTIONS:
            j = a
            break
        words.append((word, a, j))
    if len(words) >= 2 and any(len(w) >= 3 and set(w.lower()) & set("aeiouy") for w, _, _ in words):
        return words
    return []


def _italic_word_len(nodes: list[Node], k: int, step: int) -> int:
    """Length of the italic text-font word (with a lower-case letter) starting at nodes[k], reading in *step*."""
    letters = ""
    while 0 <= k < len(nodes):
        n = nodes[k]
        g = n.item
        if not (isinstance(g, Glyph) and g.ch.isalpha() and g.italic and not g.math_font and not g.bold) \
                or n.sup or n.sub:
            break
        if letters:
            prev = nodes[k - step].item
            gap = g.x0 - prev.x1 if step > 0 else prev.x0 - g.x1
            if gap > 0.17 * g.size:
                break
        letters += g.ch
        k += step
    return len(letters) if any(c.islower() for c in letters) else 0
