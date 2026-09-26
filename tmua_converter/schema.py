"""The ``.tmua.json`` output format.

Key order in the written file follows the simulator's documented format, so the
models below declare fields in that order.  No answer/solution fields exist here
on purpose: the simulator uses manual marking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FORMAT_VERSION = 1


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    content: str


class Image(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    alt: str


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    stem: str
    options: list[Option]
    images: list[Image] = Field(default_factory=list)
    sourcePage: int
    needsReview: bool = False


class Paper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formatVersion: Literal[1] = FORMAT_VERSION
    id: str
    title: str
    paper: str
    year: str
    durationMinutes: int
    questions: list[Question]


def paper_to_json_text(paper: Paper) -> str:
    """Serialise exactly as it will be written to disk.

    ``json.dumps`` escapes real newlines as ``\\n`` and backslashes as ``\\\\``
    in the raw file, which is what a JSON parser expects; after parsing the
    strings contain real newlines and single LaTeX backslashes again.
    """
    return json.dumps(paper.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def write_paper(paper: Paper, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(paper_to_json_text(paper), encoding="utf-8")
    return path


def load_paper_dict(path: str | Path) -> dict:
    """Reload a written file with a real JSON parser (used for pass 3)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
