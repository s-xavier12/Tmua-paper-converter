"""Combining several converted papers into one simulator paper."""

from __future__ import annotations

from .naming import paper_id
from .schema import Paper, Question


def combine_papers(papers: list[Paper], title: str, paper_label: str | None = None, year: str | None = None,
                   duration_minutes: int | None = None) -> Paper:
    """Concatenate papers in the given order and renumber questions 1..N.

    Source information that has no field of its own in the format is kept in
    the metadata: ``year`` lists each source paper (e.g. "2019 Paper 1 + 2020
    Paper 2") unless an explicit value is given.  ``sourcePage`` keeps each
    question's page in its own source PDF.
    """
    if not papers:
        raise ValueError("nothing to combine")
    questions: list[Question] = []
    for p in papers:
        for q in p.questions:
            questions.append(q.model_copy(update={"number": len(questions) + 1}, deep=True))
    sources = " + ".join(f"{p.year} {p.paper}".strip() for p in papers)
    return Paper(
        id=paper_id(title),
        title=title,
        paper=paper_label or "Combined",
        year=year or sources,
        durationMinutes=duration_minutes or sum(p.durationMinutes for p in papers),
        questions=questions,
    )
