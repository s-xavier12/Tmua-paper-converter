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
# Fonts LaTeX's picture mode uses to draw slanted lines and circles: graphics, not text.
_DRAWING_FONT_RE = re.compile(r"^(LINE|LCIRCLE|LCIRCLEW)\d*", re.I)
_NUMBER_ONLY_RE = re.compile(r"[\-–—\s]*\(?\d{1,3}[.):]?[\-–—\s]*")
# Lines that start with a question marker: never running headers, even when a
# question happens to start at the same height on many pages.
_QUESTION_START_RE = re.compile(r"^\s*(?:(?:Question|Q)\s*)?\d{1,3}[.):]?(?:\s|$)", re.I)
_BLANK_PAGE_RE = re.compile(r"(blank\s*page|intentionally\s*(left\s*)?blank)", re.I)


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
    number_box: "Box | None" = None  # box of a leading "12." if the line starts with one


@dataclass(frozen=True)
class Anchor:
    """A detected question-number marker (e.g. a bold ``7`` in the margin)."""

    number: int
    page: int  # 1-based
    box: Box


def _spaced_text(spans: list[dict]) -> str:
    """Line text with spaces restored from gaps (many PDFs store no space characters)."""
    out = ""
    prev_x1 = None
    for s in spans:
        size = float(s.get("size", 10)) or 10
        for c in s.get("chars", []):
            ch = clean_text(c["c"])
            if not ch:
                continue
            x0, x1 = c["bbox"][0], c["bbox"][2]
            if ch.isspace():
                if out and not out.endswith(" "):
                    out += " "
                prev_x1 = x1
                continue
            if prev_x1 is not None and x0 - prev_x1 > 0.2 * size and out and not out.endswith(" "):
                out += " "
            out += ch
            prev_x1 = x1
    return out.strip()


def clean_text(s: str) -> str:
    return s.translate(_ZERO_WIDTH)


def png_bytes(img: Image.Image, optimize: bool = True) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


class PdfDocument:
    """A PDF with a text layer for every page.

    Pages that are pictures (scans, photos) - and pictures pasted into normal
    pages - are read with free offline OCR (Tesseract) when ``ocr`` is on.
    """

    def __init__(self, path: str | Path, ocr: bool = True):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._doc = pymupdf.open(str(self.path))
        if self._doc.needs_pass:
            raise ValueError(f"{self.path.name} is password protected")
        self._lines_cache: dict[int, list[TextLine]] = {}
        self._furniture: set[tuple[str, int]] | None = None
        self._anchors: dict[int, Anchor] | None = None
        self.ocr_enabled = ocr
        self._pages: dict[int, pymupdf.Page] = {}
        self._textpages: dict[int, object] = {}
        self.ocr_areas: dict[int, list[Box]] = {}  # page -> areas whose text came from OCR
        self.ocr_missing_pages: list[int] = []  # pages that needed OCR but Tesseract is not installed
        self._ink_cache: dict[tuple[int, tuple], list[Box]] = {}

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
        if page not in self._pages:
            self._pages[page] = self._doc[page - 1]
        return self._pages[page]

    # ------------------------------------------------------------------ OCR
    def textpage(self, page: int):
        """The text layer to read: native, or OCR for pictures (None = native)."""
        if page in self._textpages:
            return self._textpages[page]
        tp = None
        with self._lock:
            pg = self._page(page)
            native = pg.get_text("text").strip()
            pr = pg.rect
            big_images = [Box.of(i["bbox"]) for i in pg.get_image_info()
                          if (i["bbox"][2] - i["bbox"][0]) > 0.25 * pr.width and
                          (i["bbox"][3] - i["bbox"][1]) > 0.03 * pr.height]
            if self.ocr_enabled and (not native or big_images):
                from .ocr import find_tessdata

                tessdata = find_tessdata()
                if tessdata is None:
                    self.ocr_missing_pages.append(page)
                elif not native:
                    tp = pg.get_textpage_ocr(dpi=300, full=True, tessdata=tessdata)
                    self.ocr_areas[page] = [Box(0, 0, pr.width, pr.height)]
                else:
                    cand = pg.get_textpage_ocr(dpi=300, full=False, tessdata=tessdata)
                    words = pg.get_text("words", textpage=cand)
                    texty = [b for b in big_images
                             if sum(1 for w in words if b.x0 <= (w[0] + w[2]) / 2 <= b.x1
                                    and b.y0 <= (w[1] + w[3]) / 2 <= b.y1 and re.search(r"[A-Za-z]{2}", w[4])) >= 4]
                    if texty:  # a picture of text (e.g. a pasted screenshot of a question)
                        tp = cand
                        self.ocr_areas[page] = texty
        self._textpages[page] = tp
        return tp

    def is_ocr(self, page: int, box: Box | None = None) -> bool:
        self.textpage(page)
        areas = self.ocr_areas.get(page, [])
        if box is None:
            return bool(areas)
        return any(a.overlap_fraction(box) > 0.5 for a in areas)

    def get_text(self, page: int, kind: str = "text", **kw):
        tp = self.textpage(page)
        with self._lock:
            if tp is not None:
                kw["textpage"] = tp
            return self._page(page).get_text(kind, **kw)

    def rawdict(self, page: int) -> dict:
        """Characters of the page: the PDF's own text, plus OCR text inside pictures of text."""
        tp = self.textpage(page)
        flags = pymupdf.TEXTFLAGS_TEXT
        with self._lock:
            pg = self._page(page)
            if tp is None:
                return pg.get_text("rawdict", flags=flags)
            ocr = pg.get_text("rawdict", flags=flags, textpage=tp)
            areas = self.ocr_areas.get(page, [])
            if len(areas) == 1 and areas[0].area >= Box.of(pg.rect).area * 0.99:
                return ocr  # the whole page is a picture
            native = pg.get_text("rawdict", flags=flags)

        def inside(line) -> bool:
            b = Box.of(line["bbox"])
            cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
            return any(a.x0 <= cx <= a.x1 and a.y0 <= cy <= a.y1 for a in areas)

        blocks = []
        for blk in native.get("blocks", []):
            lines = [ln for ln in blk.get("lines", []) if not inside(ln)]
            if lines:
                blocks.append({**blk, "lines": lines})
        for blk in ocr.get("blocks", []):
            lines = [ln for ln in blk.get("lines", []) if inside(ln)]
            if lines:
                blocks.append({**blk, "lines": lines})
        return {"blocks": blocks, "width": native.get("width"), "height": native.get("height")}

    def ink_components(self, page: int, area: Box, exclude: list[Box]) -> list[Box]:
        """Connected blobs of ink in *area* (pt), ignoring the *exclude* boxes (text).

        Used to find diagrams inside pictures, where there are no vector drawings.
        """
        key = (page, tuple(round(v, 1) for v in area.to_list()))
        if key in self._ink_cache:
            return self._ink_cache[key]
        zoom = 2.0
        cell = 3  # px; cells with any ink are joined into blobs
        img = self.render(page, zoom, area).convert("L")
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)
        for b in exclude:
            i = b.intersect(area)
            if not i.is_empty():
                draw.rectangle([(i.x0 - area.x0) * zoom - 2, (i.y0 - area.y0) * zoom - 2,
                                (i.x1 - area.x0) * zoom + 2, (i.y1 - area.y0) * zoom + 2], fill=255)
        ink = img.point(lambda v: 255 if v < 150 else 0)
        pad_w, pad_h = (-ink.width) % cell, (-ink.height) % cell
        if pad_w or pad_h:
            padded = Image.new("L", (ink.width + pad_w, ink.height + pad_h), 0)
            padded.paste(ink, (0, 0))
            ink = padded
        small = ink.reduce(cell)  # box average: any ink in a cell -> non-zero
        gw, gh = small.size
        grid = bytearray(1 if v else 0 for v in small.tobytes())
        seen = bytearray(gw * gh)
        boxes: list[Box] = []
        for start in range(gw * gh):
            if not grid[start] or seen[start]:
                continue
            stack = [start]
            seen[start] = 1
            minx = maxx = start % gw
            miny = maxy = start // gw
            while stack:
                c = stack.pop()
                cx, cy = c % gw, c // gw
                minx, maxx, miny, maxy = min(minx, cx), max(maxx, cx), min(miny, cy), max(maxy, cy)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < gw and 0 <= ny < gh:
                            n = ny * gw + nx
                            if grid[n] and not seen[n]:
                                seen[n] = 1
                                stack.append(n)
            f = cell / zoom
            boxes.append(Box(area.x0 + minx * f, area.y0 + miny * f, area.x0 + (maxx + 1) * f,
                             area.y0 + (maxy + 1) * f))
        self._ink_cache[key] = boxes
        return boxes

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
        data = self.rawdict(page)
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if not _DRAWING_FONT_RE.match(s.get("font", ""))]
                for s in spans:
                    s["text"] = "".join(c["c"] for c in s.get("chars", []))
                spans = [s for s in spans if clean_text(s["text"]).strip()]
                if not spans:
                    continue
                text = _spaced_text(spans)
                first = spans[0]
                font = first.get("font", "")
                bold = bool(first.get("flags", 0) & 16) or "bold" in font.lower() or "black" in font.lower()
                number_box = None
                lead = [c for c in first.get("chars", []) if not c["c"].isspace()]
                k = 0
                while k < len(lead) and (lead[k]["c"].isdigit() or (k and lead[k]["c"] in ".):")):
                    k += 1
                if 0 < k < len(lead) and lead[0]["c"].isdigit():
                    number_box = Box(min(c["bbox"][0] for c in lead[:k]), min(c["bbox"][1] for c in lead[:k]),
                                     max(c["bbox"][2] for c in lead[:k]), max(c["bbox"][3] for c in lead[:k]))
                out.append(TextLine(
                    text=text,
                    box=Box.of(line["bbox"]),
                    first_span_text=clean_text(first["text"]).strip(),
                    first_span_box=Box.of(first["bbox"]),
                    first_span_bold=bold,
                    size=float(first.get("size", 0.0)),
                    number_box=number_box,
                ))
        out.sort(key=lambda ln: (round(ln.box.y0, 1), ln.box.x0))
        self._lines_cache[page] = out
        return out

    def text(self, page: int) -> str:
        if self.textpage(page) is None:
            with self._lock:
                return clean_text(self._page(page).get_text("text"))
        return "\n".join(ln.text for ln in self.lines(page))

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
                if _NUMBER_ONLY_RE.fullmatch(ln.text) or _QUESTION_START_RE.match(ln.text):
                    continue  # numbers are handled by position (question markers vs page numbers)
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
        if key in self._furniture_keys() and not _QUESTION_START_RE.match(ln.text):
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
            ocr_areas = self.ocr_areas.get(page, []) if self.textpage(page) is not None else []
            for info in pg.get_image_info():
                ib = Box.of(info["bbox"])
                if not any(a.overlap_fraction(ib) > 0.5 for a in ocr_areas):
                    boxes.append(ib)  # a picture/diagram (pictures of text are handled below)
            # line-drawing font glyphs (LaTeX picture mode), merged into clusters
            pieces = [Box.of(sp["bbox"]) for b in pg.get_text("dict").get("blocks", [])
                      for ln in b.get("lines", []) for sp in ln.get("spans", [])
                      if _DRAWING_FONT_RE.match(sp.get("font", ""))]
            for piece in pieces:
                for i, other in enumerate(boxes):
                    if other.gap_to(piece) < 3:
                        boxes[i] = other.union(piece)
                        break
                else:
                    boxes.append(piece)
        # inside pictures of text, diagrams are the blobs of ink that are not mostly text
        if ocr_areas:
            text_boxes = [ln.box for ln in self.lines(page)]
            for area in ocr_areas:
                for blob in self.ink_components(page, area.clamp(pbox), []):
                    covered = sum(blob.intersect(t).area for t in text_boxes if not blob.intersect(t).is_empty())
                    if blob.area > 0 and covered / blob.area < 0.5:
                        boxes.append(blob)
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
            page_lines = self.content_lines(p)
            for ln in page_lines:
                if ln.first_span_box.x0 > pw * 0.25:
                    continue
                # a question number is the first thing on its line (not an answer value after "B")
                if any(o is not ln and o.box.x1 <= ln.first_span_box.x0 + 1 and
                       min(o.box.y1, ln.box.y1) - max(o.box.y0, ln.box.y0) > 0.5 * min(o.box.height, ln.box.height)
                       for o in page_lines):
                    continue
                box = ln.first_span_box
                m = re.fullmatch(r"(\d{1,3})[.):]?", ln.first_span_text)
                if not m:  # "1. Find ..." in one piece of text (common in Word-made PDFs)
                    m = re.match(r"(\d{1,3})[.):]?(?=\s)", ln.first_span_text)
                    if m:
                        box = ln.number_box or box
                if not m:  # "Question 7", "Q7." styles
                    m = re.match(r"(?:Question|Q)\s*(\d{1,3})(?![\d])[.):]?", ln.text)
                if not m:
                    continue
                cands.append((Anchor(int(m.group(1)), p, box), ln.first_span_bold))
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

        # A question starts a little above its number (tall maths such as integral limits or
        # fractions on the first line rise above it); the previous one ends at the same point.
        def lead(anchor) -> float:
            return max(6.0, 0.9 * anchor.box.height)

        same_page_end = nxt.box.y0 - lead(nxt) if nxt is not None and nxt.page == a.page else None
        top = a.box.y0 - lead(a)
        # A picture pasted inline sits on the text baseline, so its question number is printed at
        # the picture's bottom-left: the question then starts at the top of the picture.
        prev = anchors.get(number - 1)
        floor = prev.box.y1 + 2 if prev is not None and prev.page == a.page else 0.0
        with self._lock:
            images = [Box.of(i["bbox"]) for i in self._page(a.page).get_image_info()]
        for img in images:
            if img.x0 >= a.box.x1 - 2 and img.y0 < a.box.y0 and img.y1 >= a.box.y0 - 2 and img.y0 >= floor:
                top = min(top, img.y0 - 4)
        first = span(a.page, top, same_page_end)
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
            limit = first_marker.box.y0 - lead(first_marker) if first_marker is not None else None
            if limit is not None and content.y0 >= limit - 2:
                break  # nothing above the next question's marker: no overflow
            region = span(p, content.y0 - 6, limit)
            if region is not None:
                regions.append((p, region))
            if first_marker is not None:
                break
        return regions
