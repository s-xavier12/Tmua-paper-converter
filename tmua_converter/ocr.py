"""Reading pictures of text: scanned pages, photos, screenshots (free, offline OCR).

Uses Tesseract through PyMuPDF.  Nothing leaves the computer and nothing is
paid for.  OCR reads prose well but is weak on maths symbols, so questions
read this way are always flagged for review.
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

import pymupdf

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}

INSTALL_HELP = ("Reading pictures needs the free Tesseract OCR program. Install it, then try again:\n"
                "  Windows: https://github.com/UB-Mannheim/tesseract/wiki (run the installer)\n"
                "  macOS:   brew install tesseract\n"
                "  Linux:   sudo apt install tesseract-ocr\n"
                "If it is installed somewhere unusual, set TESSDATA_PREFIX to its 'tessdata' folder.")


class OcrUnavailable(RuntimeError):
    pass


def is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def find_tessdata() -> str | None:
    """Locate Tesseract's language data folder (needed by PyMuPDF's OCR)."""
    env = os.environ.get("TESSDATA_PREFIX")
    if env and Path(env).is_dir():
        return env
    try:
        found = pymupdf.get_tessdata()
        if found:
            return found
    except Exception:  # noqa: BLE001 - older PyMuPDF or not installed
        pass
    candidates = []
    for pattern in ("/usr/share/tesseract-ocr/*/tessdata", "/usr/share/tessdata", "/usr/local/share/tessdata",
                    "/opt/homebrew/share/tessdata", "/usr/local/Cellar/tesseract/*/share/tessdata",
                    r"C:\Program Files\Tesseract-OCR\tessdata", r"C:\Program Files (x86)\Tesseract-OCR\tessdata"):
        candidates += glob.glob(pattern)
    exe = shutil.which("tesseract")
    if exe:
        candidates.append(str(Path(exe).resolve().parent.parent / "share" / "tessdata"))
        candidates.append(str(Path(exe).resolve().parent / "tessdata"))
    for c in candidates:
        if Path(c, "eng.traineddata").exists():
            return c
    return None


def require_tessdata() -> str:
    t = find_tessdata()
    if not t:
        raise OcrUnavailable(INSTALL_HELP)
    return t


def images_to_pdf(paths: list[str | Path], out: str | Path) -> Path:
    """Put one or more pictures into a PDF, one picture per page, in the given order."""
    doc = pymupdf.open()
    for p in paths:
        img = pymupdf.open(str(p))
        pdf_bytes = img.convert_to_pdf()
        img.close()
        doc.insert_pdf(pymupdf.open("pdf", pdf_bytes))
    out = Path(out)
    doc.save(str(out))
    doc.close()
    return out
