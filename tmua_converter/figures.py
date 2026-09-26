"""Cropping figures straight from the rendered PDF.

Figures are never redrawn: every image is a crop of the rendered source page.
Claude proposes a rough box; this module then tightens it deterministically:

1. snap to the vector drawings / raster images the box overlaps,
2. pull in short labels (A, B, x, 45°, ...) sitting next to those graphics,
3. push edges off long lines of question text straddling the top/bottom edge,
4. grow any edge that still cuts through ink (so nothing is cut off),
5. trim surrounding whitespace and add a small uniform margin.

The resulting crop is shown back to Claude for a visual check (pipeline).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .pdf import Box, PdfDocument, png_bytes

OUTPUT_DPI = 220
OUTPUT_ZOOM = OUTPUT_DPI / 72.0
INK_THRESHOLD = 225  # grey level below which a pixel counts as ink
PAD_PT = 5.0
MAX_GROW_PT = 40.0
GROW_STEP_PT = 3.0
LABEL_MAX_CHARS = 6
LABEL_GAP_PT = 9.0


@dataclass
class CropResult:
    page: int
    box_pt: Box
    image: Image.Image
    png: bytes
    notes: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def data_uri(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")


def _ink_mask(img: Image.Image) -> Image.Image:
    """Binary mask: 255 where there is ink."""
    return img.convert("L").point(lambda v: 255 if v < INK_THRESHOLD else 0)


def _is_long_text(text: str) -> bool:
    return len(text.replace(" ", "")) > 12


def _ink_without_text(doc: PdfDocument, page: int, region: Box, keep: Box) -> Image.Image:
    """Ink mask of *region* with long question-text lines outside *keep* erased.

    Used so edge-growing never walks into the surrounding question text.
    """
    img = _ink_mask(doc.render(page, OUTPUT_ZOOM, region))
    draw = ImageDraw.Draw(img)
    for ln in doc.content_lines(page):
        if not _is_long_text(ln.text) or keep.contains(ln.box, tol=0.5):
            continue
        b = ln.box.intersect(region)
        if b.is_empty():
            continue
        draw.rectangle([(b.x0 - region.x0) * OUTPUT_ZOOM - 1, (b.y0 - region.y0) * OUTPUT_ZOOM - 1,
                        (b.x1 - region.x0) * OUTPUT_ZOOM + 1, (b.y1 - region.y0) * OUTPUT_ZOOM + 1], fill=0)
    return img


def _edge_crossing(doc: PdfDocument, page: int, box: Box) -> dict[str, bool]:
    """Which edges of *box* have ink (other than question text) running across them."""
    margin = 3.0
    zoom = OUTPUT_ZOOM
    outer = box.expand(margin).clamp(doc.page_box(page))
    img = _ink_without_text(doc, page, outer, keep=box)
    # dilate slightly so anti-aliased strokes touching the edge still connect
    img = img.filter(ImageFilter.MaxFilter(3))
    ox, oy = outer.x0, outer.y0

    def px(v: float, origin: float) -> int:
        return int(round((v - origin) * zoom))

    w, h = img.size
    x0, x1 = max(0, px(box.x0, ox)), min(w - 1, px(box.x1, ox))
    y0, y1 = max(0, px(box.y0, oy)), min(h - 1, px(box.y1, oy))

    def crosses(inside: tuple[int, int, int, int], outside: tuple[int, int, int, int], horizontal: bool) -> bool:
        a = img.crop(inside)
        b = img.crop(outside)
        if a.getbbox() is None or b.getbbox() is None:
            return False
        size_a = (a.width, 1) if horizontal else (1, a.height)
        size_b = (b.width, 1) if horizontal else (1, b.height)
        a = a.resize(size_a, Image.Resampling.BOX).point(lambda v: 255 if v > 0 else 0)
        b = b.resize(size_b, Image.Resampling.BOX).point(lambda v: 255 if v > 0 else 0)
        return ImageChops.multiply(a, b).getbbox() is not None

    band = 2
    return {
        "top": y0 - band >= 0 and crosses((x0, y0, x1, y0 + band), (x0, y0 - band, x1, y0), True),
        "bottom": y1 + band < h and crosses((x0, y1 - band, x1, y1), (x0, y1, x1, y1 + band), True),
        "left": x0 - band >= 0 and crosses((x0, y0, x0 + band, y1), (x0 - band, y0, x0, y1), False),
        "right": x1 + band < w and crosses((x1 - band, y0, x1, y1), (x1, y0, x1 + band, y1), False),
    }


def refine_box(doc: PdfDocument, page: int, rough: Box) -> tuple[Box, list[str]]:
    """Tighten/extend a rough figure box (PDF points) to the real figure."""
    notes: list[str] = []
    pbox = doc.page_box(page)
    rough = rough.clamp(pbox)
    if rough.is_empty():
        raise ValueError("figure box lies outside the page")

    # 1. the drawings/images the rough box substantially overlaps form the core
    graphics = doc.graphic_boxes(page)
    used: list[Box] = []
    core: Box | None = None
    changed = True
    while changed:
        changed = False
        probe = rough if core is None else rough.union(core)
        for g in graphics:
            if g in used:
                continue
            if probe.overlap_fraction(g) >= 0.15 and g.area <= 8 * max(rough.area, 1.0):
                used.append(g)
                core = g if core is None else core.union(g)
                changed = True

    lines = doc.content_lines(page)
    if core is not None:
        # 2. rebuild the box around the graphics: text inside the core (table
        #    cells, labels inside a diagram) plus short labels next to it.  A
        #    label a little further away is kept only if the rough box covered it.
        box = core
        for ln in lines:
            short = not _is_long_text(ln.text) and len(ln.text.replace(" ", "")) <= LABEL_MAX_CHARS
            gap = min(g.gap_to(ln.box) for g in used)
            if core.overlap_fraction(ln.box) > 0.5 and (short or core.contains(ln.box, tol=1.0)):
                box = box.union(ln.box)
            elif short and gap <= LABEL_GAP_PT:
                box = box.union(ln.box)
                notes.append(f"included label '{ln.text}'")
            elif short and gap <= 2.5 * LABEL_GAP_PT and rough.overlap_fraction(ln.box) > 0.5:
                box = box.union(ln.box)
                notes.append(f"included label '{ln.text}'")
        if not rough.contains(box, tol=1.0):
            notes.append("extended to the full drawing and its labels")
        if box.area < rough.area * 0.8:
            notes.append("tightened to the drawing and its labels")
    else:
        # No vector/raster graphics found (e.g. scanned page): trust the rough
        # box but push its top/bottom edges off straddling lines of text.
        box = rough
        for ln in lines:
            if not _is_long_text(ln.text) or box.contains(ln.box, tol=0.5) or box.intersect(ln.box).is_empty():
                continue
            if ln.box.y0 < box.y0 <= ln.box.y1:
                box = Box(box.x0, ln.box.y1 + 0.5, box.x1, box.y1)
                notes.append(f"moved top edge below text '{ln.text[:40]}'")
            elif ln.box.y0 <= box.y1 < ln.box.y1:
                box = Box(box.x0, box.y0, box.x1, ln.box.y0 - 0.5)
                notes.append(f"moved bottom edge above text '{ln.text[:40]}'")

    # 3. grow edges that still cut through ink (never into question text)
    grown = {"top": 0.0, "bottom": 0.0, "left": 0.0, "right": 0.0}
    for _ in range(int(MAX_GROW_PT / GROW_STEP_PT)):
        crossing = _edge_crossing(doc, page, box)
        todo = [e for e, c in crossing.items() if c and grown[e] < MAX_GROW_PT]
        if not todo:
            break
        x0, y0, x1, y1 = box.x0, box.y0, box.x1, box.y1
        for e in todo:
            grown[e] += GROW_STEP_PT
            if e == "top":
                y0 -= GROW_STEP_PT
            elif e == "bottom":
                y1 += GROW_STEP_PT
            elif e == "left":
                x0 -= GROW_STEP_PT
            else:
                x1 += GROW_STEP_PT
        box = Box(x0, y0, x1, y1).clamp(pbox)
    for e, g in grown.items():
        if g:
            notes.append(f"grew {e} edge by {g:.0f}pt so ink is not cut off")
    return box, notes


def trim_box(doc: PdfDocument, page: int, box: Box, pad: float = PAD_PT) -> Box:
    """Shrink *box* to the ink it contains, plus a uniform margin."""
    img = _ink_without_text(doc, page, box, keep=box)
    bb = img.getbbox()
    if bb is None:
        return box
    inner = Box(box.x0 + bb[0] / OUTPUT_ZOOM, box.y0 + bb[1] / OUTPUT_ZOOM,
                box.x0 + bb[2] / OUTPUT_ZOOM, box.y0 + bb[3] / OUTPUT_ZOOM)
    return inner.expand(pad).clamp(doc.page_box(page))


def _maybe_grayscale(img: Image.Image) -> Image.Image:
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    diff = ImageChops.difference(r, g).getextrema()[1], ImageChops.difference(g, b).getextrema()[1]
    return rgb.convert("L") if max(diff) <= 12 else rgb


def crop_metrics(doc: PdfDocument, page: int, box: Box, img: Image.Image) -> dict:
    ink = _ink_mask(img).getbbox()
    area = img.width * img.height
    ink_area = (ink[2] - ink[0]) * (ink[3] - ink[1]) if ink else 0
    crossing = _edge_crossing(doc, page, box)
    pbox = doc.page_box(page)
    return {
        "width_px": img.width,
        "height_px": img.height,
        "whitespace_fraction": round(1 - ink_area / area, 3) if area else 1.0,
        "page_area_fraction": round(box.area / pbox.area, 3),
        "page_height_fraction": round(box.height / pbox.height, 3),
        "edges_cutting_ink": [e for e, c in crossing.items() if c],
        "empty": ink is None,
    }


def crop_figure(doc: PdfDocument, page: int, rough_pt: Box, refine: bool = True) -> CropResult:
    """Produce the final PNG crop for a figure whose rough box is *rough_pt*."""
    notes: list[str] = []
    box = rough_pt.clamp(doc.page_box(page))
    if refine:
        box, notes = refine_box(doc, page, box)
    box = trim_box(doc, page, box)
    img = _maybe_grayscale(doc.render(page, OUTPUT_ZOOM, box))
    return CropResult(page=page, box_pt=box, image=img, png=png_bytes(img), notes=notes,
                      metrics=crop_metrics(doc, page, box, img))


def overlay_box(img: Image.Image, box_px: Box, color=(230, 0, 0), width: int = 4) -> Image.Image:
    """Copy of a page image with a rectangle drawn on it (for visual checks)."""
    out = img.convert("RGB").copy()
    ImageDraw.Draw(out).rectangle([box_px.x0, box_px.y0, box_px.x1, box_px.y1], outline=color, width=width)
    return out


def decode_data_uri(src: str) -> bytes:
    prefix = "data:image/png;base64,"
    if not src.startswith(prefix):
        raise ValueError("not a PNG data URI")
    return base64.b64decode(src[len(prefix):], validate=True)
