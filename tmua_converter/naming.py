"""File names and ids for converted papers."""

from __future__ import annotations

import re
import unicodedata


def _words(title: str, paper: str | None) -> list[str]:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    if paper and paper.strip() and paper.lower().replace(" ", "") not in text.lower().replace(" ", ""):
        text = f"{text} {paper}"
    return re.findall(r"[A-Za-z0-9]+", text)


def paper_filename(title: str, paper: str | None = None) -> str:
    """'Hercules Set 2', 'Paper 1' -> 'Hercules_Set_2_Paper_1.tmua.json'."""
    words = _words(title, paper) or ["paper"]
    return "_".join(words) + ".tmua.json"


def paper_id(title: str, paper: str | None = None) -> str:
    """'Hercules Set 2', 'Paper 1' -> 'hercules-set-2-paper-1'."""
    words = _words(title, paper) or ["paper"]
    return "-".join(w.lower() for w in words)
