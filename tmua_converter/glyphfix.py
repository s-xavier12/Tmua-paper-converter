"""Repair characters whose Unicode the PDF records wrongly.

Some PDFs (often Word with Cambria Math) have no correct Unicode for the small digits and letters
used in fractions and powers; readers then report the glyph number itself as the character, so
"1" in a fraction comes out as an Oriya or Malayalam letter.  The glyph number is still in the PDF,
so the real character is recovered from the embedded font's own character table, or, for Word's
maths font, from its fixed layout of script-size glyphs.
"""

from __future__ import annotations

import unicodedata

# Cambria Math script-size (ssty 1) and scriptscript-size (ssty 2) glyphs, in the font's fixed order.
_CAMBRIA: dict[int, str] = {}
for _k, _c in enumerate("0123456789+−"):
    _CAMBRIA[2868 + _k] = _c   # script size (fractions, exponents)
    _CAMBRIA[3116 + _k] = _c   # scriptscript size (exponents of exponents)
for _k in range(26):
    _CAMBRIA[3028 + _k] = chr(ord("a") + _k)   # script-size italic letters
    _CAMBRIA[3276 + _k] = chr(ord("a") + _k)   # scriptscript-size italic letters
for _k, _c in enumerate("0123456789"):
    _CAMBRIA[882 + _k] = _c    # full-size digits (used when the font's own table was stripped)
_CAMBRIA.update({3397: "+", 3398: "−", 3404: "=", 4666: "(", 4667: ")"})
_CAMBRIA.update({3095: "π", 3427: "[", 3431: "]", 3435: "(", 3439: ")", 3505: "∫", 3628: "|",
                 4672: "(", 4673: ")"})


def _key(name: str) -> str:
    """Font names compare without subset prefix, spaces, hyphens or case."""
    return "".join(ch for ch in name.split("+")[-1].lower() if ch.isalnum())


def plausible(c: str) -> bool:
    """Characters an English maths paper uses; anything else is a sign of a broken text layer."""
    if len(c) != 1:
        return True
    o = ord(c)
    if o < 0x250 or 0x370 <= o < 0x400 or 0x2000 <= o < 0x2C00 or 0x1D400 <= o < 0x1D800:
        return True
    if 0xFB00 <= o < 0xFB07 or o in (0x2E3A, 0x3001, 0x3008, 0x3009):
        return True
    return unicodedata.category(c) in ("Zs",)


def repair_for(doc) -> "GlyphRepair":
    """One GlyphRepair per open document (it caches fonts and glyph traces)."""
    rep = getattr(doc, "_tmua_glyph_repair", None)
    if rep is None:
        rep = GlyphRepair(doc)
        try:
            doc._tmua_glyph_repair = rep
        except AttributeError:
            pass
    return rep


class GlyphRepair:
    """Per-document: maps (font name, glyph id) to the real character."""

    def __init__(self, doc):
        self.doc = doc
        self._fonts: dict[str, dict[int, str] | None] = {}
        self._trace: dict[int, dict[tuple[int, int], tuple[str, int]]] = {}
        self._font_is_cambria: dict[str, bool] = {}
        self._math: dict[str, bool] = {}

    def _reverse_cmap(self, fontname: str, page) -> dict[int, str] | None:
        if fontname in self._fonts:
            return self._fonts[fontname]
        table = None
        for f in page.get_fonts():
            if _key(f[3]) == _key(fontname) or _key(f[3]).startswith(_key(fontname)):
                try:
                    import pymupdf

                    buf = self.doc.extract_font(f[0])[3]
                    font = pymupdf.Font(fontbuffer=buf) if buf else None
                except Exception:  # noqa: BLE001 - unreadable font: no table
                    font = None
                if font is not None:
                    self._font_is_cambria[_key(fontname)] = "cambria" in font.name.lower() \
                        or font.glyph_count == 7614 or font.has_glyph(0x30) == 882
                    table = {}
                    for rng in ((0x20, 0x250), (0x370, 0x400), (0x2000, 0x2C00), (0x1D400, 0x1D800)):
                        for o in range(*rng):
                            gid = font.has_glyph(o)
                            if gid and gid not in table:
                                table[gid] = chr(o)
                break
        self._fonts[fontname] = table
        return table

    def is_math_font(self, fontname: str, page) -> bool:
        """A maths font under any name: its file has maths italic letters (Cambria Math, STIX, ...)."""
        key = _key(fontname)
        if key not in self._math:
            self._math[key] = False
            for f in page.get_fonts():
                if _key(f[3]) == key:
                    try:
                        import pymupdf

                        buf = self.doc.extract_font(f[0])[3]
                        font = pymupdf.Font(fontbuffer=buf) if buf else None
                        self._math[key] = bool(font and (font.has_glyph(0x1D465) or font.has_glyph(0x1D44E)
                                                         or "math" in font.name.lower()))
                    except Exception:  # noqa: BLE001
                        pass
                    break
        return self._math[key]

    def _glyph_ids(self, page) -> dict[tuple[int, int], tuple[str, int]]:
        key = page.number
        if key not in self._trace:
            ids = {}
            try:
                for span in page.get_texttrace():
                    for c in span["chars"]:
                        ids[(round(c[2][0] * 4), round(c[2][1] * 4))] = (span["font"], c[1])
            except Exception:  # noqa: BLE001
                pass
            self._trace[key] = ids
        return self._trace[key]

    def fix(self, page, c: str, origin: tuple[float, float], fontname: str) -> str | None:
        """The real character for an implausible *c* drawn at *origin*, or None if unknown."""
        hit = self._glyph_ids(page).get((round(origin[0] * 4), round(origin[1] * 4)))
        if hit is None:
            return None
        font, gid = hit
        table = self._reverse_cmap(font, page) or {}
        if gid in table:
            return table[gid]
        # Word's maths font, whatever the PDF calls it ("CambriaMath", "CIDFont+F4", ...): recognised by
        # its name inside the font file, by its glyph layout, or by the broken PDF reporting the glyph
        # number itself as the character
        cambria = ("cambriamath" in _key(font) or self._font_is_cambria.get(_key(font))
                   or ord(c) == gid)
        if cambria:
            return _CAMBRIA.get(gid)
        return None
