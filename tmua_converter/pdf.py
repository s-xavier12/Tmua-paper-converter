"""PDF access: page rendering, text layer, question anchors and graphics.

Coordinates
-----------
* PDF space is in points (1/72 inch), origin top-left (PyMuPDF convention).
* "Model pixels" are pixels of the page image sent to Claude, rendered so the
  long edge is ``MODEL_LONG_EDGE`` px.  Claude's coordinates map 1:1 to those
  pixels, so converting between the two spaces is a single scale factor.

PyMuPDF documents are not thread-safe, so every access goes through a lock.
"""

from __future__ import annotations

import io
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image

MODEL_LONG_EDGE = 2576  # max image long edge used by current Claude vision models

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_NUMBER_ONLY_RE = re.compile(r"[\-–—\s]*\d{1,3}[\-–—\s]*")
_BLANK_PAGE_RE = re.compile(r"(blank\s+page|intentionally\s+(left\s+)?blank)", re.I)


@dataclass(frozen=True)
class Box:
    """Axis-aligned rectangle (x0, y0, x1, y1)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def union(self, other: "Box") -> "Box":
        return Box(min(self.x0, other.x0), min(self.y0, other.y0), max(self.x1, other.x1), max(self.y1, other.y1))

    def intersect(self, other: "Box") -> "Box":
        return Box(max(self.x0, other.x0), max(self.y0, other.y0), min(self.x1, other.x1), min(self.y1, other.y1))

    def expand(self, dx: float, dy: float | None = None) -> "Box":
        dy = dx if dy is None else dy
        return Box(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def clamp(self, bounds: "Box") -> "Box":
        return self.intersect(bounds)

    def scale(self, f: float) -> "Box":
        return Box(self.x0 * f, self.y0 * f, self.x1 * f, self.y1 * f)

    def contains(self, other: "Box", tol: float = 0.0) -> bool:
        return (other.x0 >= self.x0 - tol and other.y0 >= self.y0 - tol
                and other.x1 <= self.x1 + tol and other.y1 <= self.y1 + tol)

    def overlap_fraction(self, other: "Box") -> float:
        """Fraction of *other*'s area that lies inside self."""
        inter = self.intersect(other)
        if inter.is_empty() or other.area == 0:
            return 0.0
        return inter.area / other.area

    def gap_to(self, other: "Box") -> float:
        """Distance between two boxes (0 if they touch or overlap)."""
        dx = max(0.0, max(self.x0, other.x0) - min(self.x1, other.x1))
        dy = max(0.0, max(self.y0, other.y0) - min(self.y1, other.y1))
        return (dx * dx + dy * dy) ** 0.5

    def to_list(self, ndigits: int = 1) -> list[float]:
        return [round(v, ndigits) for v in (self.x0, self.y0, self.x1, self.y1)]

    @classmethod
    def of(cls, r) -> "Box":
        return cls(float(r[0]), float(r[1]), float(r[2]), float(r[3]))


@dataclass(frozen=True)
class TextLine:
    text: str
    box: Box
    first_span_text: str
    first_span_box: Box
    first_span_bold: bool
    size: float


@dataclass(frozen=True)
class Anchor:
    """A detected question-number marker (e.g. a bold ``7`` in the margin)."""

    number: int
    page: int  # 1-based
    box: Box


def clean_text(s: str) -> str:
    return s.translate(_ZERO_WIDTH)


def png_bytes(img: Image.Image, optimize: bool = True) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


class PdfDocument:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._doc = pymupdf.open(str(self.path))
        if self._doc.needs_pass:
            raise ValueError(f"{self.path.name} is password protected")
        self._lines_cache: dict[int, list[TextLine]] = {}
        self._furniture: set[tuple[str, int]] | None = None
        self._anchors: dict[int, Anchor] | None = None

    # ------------------------------------------------------------------ basics
    @property
    def page_count(self) -> int:
        return self._doc.page_count

    def close(self) -> None:
        with self._lock:
            self._doc.close()

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _page(self, page: int) -> pymupdf.Page:
        if not 1 <= page <= self.page_count:
            raise ValueError(f"page {page} out of range 1..{self.page_count}")
        return self._doc[page - 1]

    def page_box(self, page: int) -> Box:
        with self._lock:
            r = self._page(page).rect
            return Box(0.0, 0.0, float(r.width), float(r.height))

    # ------------------------------------------------------- coordinate spaces
    def model_zoom(self, page: int) -> float:
        """Pixels per point for the full-page image sent to Claude."""
        b = self.page_box(page)
        return MODEL_LONG_EDGE / max(b.width, b.height)

    def model_image_size(self, page: int) -> tuple[int, int]:
        b = self.page_box(page)
        z = self.model_zoom(page)
        return round(b.width * z), round(b.height * z)

    def px_to_pt(self, page: int, box_px: Box) -> Box:
        return box_px.scale(1.0 / self.model_zoom(page))

    def pt_to_px(self, page: int, box_pt: Box) -> Box:
        return box_pt.scale(self.model_zoom(page))

    # --------------------------------------------------------------- rendering
    def render(self, page: int, zoom: float, clip: Box | None = None) -> Image.Image:
        with self._lock:
            pg = self._page(page)
            kwargs = {"matrix": pymupdf.Matrix(zoom, zoom), "alpha": False}
            if clip is not None:
                c = clip.clamp(self.page_box(page))
                if c.is_empty():
                    raise ValueError("clip region is empty")
                kwargs["clip"] = pymupdf.Rect(c.x0, c.y0, c.x1, c.y1)
            pix = pg.get_pixmap(**kwargs)
            mode = "RGB" if pix.n >= 3 else "L"
            return Image.frombytes(mode, (pix.width, pix.height), pix.samples)

    def render_model_page(self, page: int) -> Image.Image:
        """Full page at the resolution Claude sees (long edge = MODEL_LONG_EDGE)."""
        return self.render(page, self.model_zoom(page))

    def render_region_for_model(self, page: int, clip_pt: Box, max_zoom: float = 8.0) -> Image.Image:
        """Zoomed render of a region, as large as the model accepts."""
        clip_pt = clip_pt.clamp(self.page_box(page))
        zoom = min(max_zoom, MODEL_LONG_EDGE / max(clip_pt.width, clip_pt.height, 1.0))
        return self.render(page, zoom, clip_pt)

    # -------------------------------------------------------------- text layer
    def lines(self, page: int) -> list[TextLine]:
        if page in self._lines_cache:
            return self._lines_cache[page]
        out: list[TextLine] = []
        with self._lock:
            data = self._page(page).get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if clean_text(s.get("text", "")).strip()]
                if not spans:
                    continue
                text = clean_text("".join(s["text"] for s in spans)).strip()
                first = spans[0]
                font = first.get("font", "")
                bold = bool(first.get("flags", 0) & 16) or "bold" in font.lower() or "black" in font.lower()
                out.append(TextLine(
                    text=text,
                    box=Box.of(line["bbox"]),
                    first_span_text=clean_text(first["text"]).strip(),
                    first_span_box=Box.of(first["bbox"]),
                    first_span_bold=bold,
                    size=float(first.get("size", 0.0)),
                ))
        out.sort(key=lambda ln: (round(ln.box.y0, 1), ln.box.x0))
        self._lines_cache[page] = out
        return out

    def text(self, page: int) -> str:
        with self._lock:
            return clean_text(self._page(page).get_text("text"))

    def has_text_layer(self) -> bool:
        return any(self.lines(p) for p in range(1, self.page_count + 1))

    def _furniture_keys(self) -> set[tuple[str, int]]:
        """Running headers/footers: lines repeated at the same height on many pages."""
        if self._furniture is not None:
            return self._furniture
        counts: Counter[tuple[str, int]] = Counter()
        for p in range(1, self.page_count + 1):
            seen = set()
            for ln in self.lines(p):
                if _NUMBER_ONLY_RE.fullmatch(ln.text):
                    continue  # bare numbers are handled by position (question markers vs page numbers)
                key = (re.sub(r"\d+", "#", ln.text.lower()), round(ln.box.y0 / 6))
                if key not in seen:
                    seen.add(key)
                    counts[key] += 1
        threshold = max(3, int(self.page_count * 0.5 + 0.5))
        self._furniture = {k for k, c in counts.items() if c >= threshold} if self.page_count >= 3 else set()
        return self._furniture

    def is_furniture(self, ln: TextLine, page: int) -> bool:
        box = self.page_box(page)
        in_margin = ln.box.y1 < box.height * 0.09 or ln.box.y0 > box.height * 0.91
        key = (re.sub(r"\d+", "#", ln.text.lower()), round(ln.box.y0 / 6))
        if key in self._furniture_keys():
            return True
        # Bare page numbers sit in the top/bottom margin away from the left edge;
        # a bare number at the left margin is a question marker, not furniture.
        return in_margin and bool(_NUMBER_ONLY_RE.fullmatch(ln.text)) and ln.box.x0 > box.width * 0.25

    def content_lines(self, page: int) -> list[TextLine]:
        return [ln for ln in self.lines(page) if not self.is_furniture(ln, page)]

    def is_blank_page(self, page: int) -> bool:
        """True for 'BLANK PAGE' pages and pages with no text or graphics at all."""
        content = self.content_lines(page)
        if content and all(_BLANK_PAGE_RE.search(ln.text) for ln in content):
            return True
        if content or self.graphic_boxes(page):
            return False
        # No text layer: fall back to pixels (scanned pages).
        img = self.render(page, 0.5).convert("L")
        dark = sum(img.histogram()[:200])
        return dark < img.width * img.height * 0.002

    # ---------------------------------------------------------------- graphics
    def graphic_boxes(self, page: int) -> list[Box]:
        """Vector-drawing clusters and raster images, minus rules/frames/fraction bars."""
        with self._lock:
            pg = self._page(page)
            pbox = self.page_box(page)
            boxes: list[Box] = []
            try:
                clusters = pg.cluster_drawings()
            except Exception:  # very old PyMuPDF or malformed drawings
                clusters = [d["rect"] for d in pg.get_drawings()]
            for r in clusters:
                boxes.append(Box.of(r))
            for info in pg.get_image_info():
                boxes.append(Box.of(info["bbox"]))
        keep = []
        for b in boxes:
            b = b.clamp(pbox)
            if b.is_empty():
                continue
            if min(b.width, b.height) < 3.0:  # rules, underlines, fraction bars
                continue
            if b.width > pbox.width * 0.92 and b.height > pbox.height * 0.8:  # page frame
                continue
            if b.width > pbox.width * 0.92 and b.height < 12:  # header/footer bands
                continue
            keep.append(b)
        return keep

    def content_box(self, page: int) -> Box | None:
        """Union of all non-furniture text and graphics on the page."""
        boxes = [ln.box for ln in self.content_lines(page)] + self.graphic_boxes(page)
        if not boxes:
            return None
        out = boxes[0]
        for b in boxes[1:]:
            out = out.union(b)
        return out

    # --------------------------------------------------------- question anchors
    def find_question_anchors(self) -> dict[int, Anchor]:
        """Detect question-number markers from the text layer.

        Candidates are lines whose first span is a 1-3 digit number (optionally
        followed by '.' or ')') in the left quarter of the page.  Candidates are
        grouped into columns by x position; in each column numbers are accepted
        greedily in reading order as 1, 2, 3, ...  The column producing the
        longest run wins (ties: more bold markers, then leftmost), which rejects
        stray digits from maths such as exponents and fraction parts.
        """
        if self._anchors is not None:
            return self._anchors
        cands: list[tuple[Anchor, bool]] = []
        for p in range(1, self.page_count + 1):
            pw = self.page_box(p).width
            for ln in self.content_lines(p):
                if ln.first_span_box.x0 > pw * 0.25:
                    continue
                m = re.fullmatch(r"(\d{1,3})[.):]?", ln.first_span_text)
                if not m:  # "Question 7", "Q7." styles
                    m = re.match(r"(?:Question|Q)\s*(\d{1,3})(?![\d])[.):]?", ln.text)
                if not m:
                    continue
                cands.append((Anchor(int(m.group(1)), p, ln.first_span_box), ln.first_span_bold))
        best: tuple[tuple[int, int, float], dict[int, Anchor]] | None = None
        for col in sorted({round(a.box.x0 / 8) for a, _ in cands}):
            column = sorted((c for c in cands if abs(round(c[0].box.x0 / 8) - col) <= 1),
                            key=lambda c: (c[0].page, c[0].box.y0))
            run: dict[int, Anchor] = {}
            bold = 0
            for a, is_bold in column:
                if a.number == len(run) + 1:
                    run[a.number] = a
                    bold += is_bold
            score = (len(run), bold, -col)
            if run and (best is None or score > best[0]):
                best = (score, run)
        self._anchors = best[1] if best else {}
        return self._anchors

    def question_regions(self, number: int) -> list[tuple[int, Box]]:
        """Best-effort regions (page, box in pt) occupied by question *number*.

        Runs from this question's marker to the next question's marker.  When
        the next marker is on a later page, material at the top of the
        following page (above its first marker) is treated as overflow.
        Returns [] when the question has no anchor.
        """
        anchors = self.find_question_anchors()
        a = anchors.get(number)
        if a is None:
            return []
        nxt = anchors.get(number + 1)
        regions: list[tuple[int, Box]] = []

        def span(page: int, top: float, bottom: float | None) -> Box | None:
            content = self.content_box(page)
            if content is None:
                return None
            pbox = self.page_box(page)
            bottom = content.y1 + 4 if bottom is None else bottom
            if bottom - top < 4:
                return None
            left = max(0.0, min(content.x0, a.box.x0 if page == a.page else content.x0) - 6)
            return Box(left, max(0.0, top), min(pbox.width, content.x1 + 6), min(pbox.height, bottom))

        same_page_end = nxt.box.y0 - 6 if nxt is not None and nxt.page == a.page else None
        first = span(a.page, a.box.y0 - 6, same_page_end)
        if first is not None:
            regions.append((a.page, first))
        if same_page_end is not None:
            return regions

        last_page = nxt.page if nxt is not None else min(self.page_count, a.page + 1)
        for p in range(a.page + 1, last_page + 1):
            if self.is_blank_page(p):
                break
            content = self.content_box(p)
            if content is None:
                continue
            first_marker = min((x for x in anchors.values() if x.page == p), key=lambda x: x.box.y0, default=None)
            limit = first_marker.box.y0 - 3 if first_marker is not None else None
            if limit is not None and content.y0 >= limit - 2:
                break  # nothing above the next question's marker: no overflow
            region = span(p, content.y0 - 6, limit)
            if region is not None:
                regions.append((p, region))
            if first_marker is not None:
                break
        return regions
