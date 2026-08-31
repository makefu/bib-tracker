"""Deciding whether a provider's answer is about the book we asked about.

A wrong match poisons a work's cover, price and rating permanently, and is
much harder to notice than no match at all. So the thresholds are deliberately
conservative and the middle band asks a person rather than guessing.
"""

from __future__ import annotations

import difflib

from ..library.identity import normalise
from .base import MediaQuery, ProviderCandidate

#: Accept without asking.
AUTO_ACCEPT = 0.90
#: Below this, discard. Between the two, queue for confirmation.
NEEDS_CONFIRMATION = 0.70

TITLE_WEIGHT = 0.7
AUTHOR_WEIGHT = 0.3
#: Editions and reprints drift a couple of years from the catalogue's date.
YEAR_TOLERANCE = 2


def title_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, normalise(left), normalise(right)).ratio()


def author_similarity(expected: str | None, candidates: list[str]) -> float | None:
    """Token overlap, because catalogues disagree about name order.

    Returns None when there is nothing to compare. That is not the same as a
    score of zero, and not the same as a neutral half mark either: scoring an
    absent author at 0.5 would cap a perfect title match at 0.85 and make
    auto-acceptance impossible for every record without an author.
    """
    if not expected or not candidates:
        return None

    expected_tokens = set(normalise(expected).split())
    if not expected_tokens:
        return None

    best = 0.0
    for candidate in candidates:
        tokens = set(normalise(candidate).split())
        if not tokens:
            continue
        overlap = len(expected_tokens & tokens) / len(expected_tokens | tokens)
        best = max(best, overlap)
    return best


def score(query: MediaQuery, candidate: ProviderCandidate) -> float:
    """0 to 1, with a hard gate on a contradicting year."""
    if query.published_year and candidate.year and abs(query.published_year - candidate.year) > YEAR_TOLERANCE:
        return 0.0

    if query.isbn and candidate.isbn13 and query.isbn == candidate.isbn13:
        # An ISBN identifies an edition outright; nothing else can outrank it.
        return 1.0

    title = title_similarity(query.title, candidate.title)
    author = author_similarity(query.author, candidate.authors)
    if author is None:
        # Judge on what can actually be compared, rather than dragging the
        # score down for information neither side has.
        return title

    return TITLE_WEIGHT * title + AUTHOR_WEIGHT * author


def rank(query: MediaQuery, candidates: list[ProviderCandidate]) -> list[ProviderCandidate]:
    for candidate in candidates:
        candidate.score = score(query, candidate)
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def choose(query: MediaQuery, candidates: list[ProviderCandidate]) -> tuple[ProviderCandidate | None, bool]:
    """Return the best candidate and whether it needs a person to confirm it."""
    ranked = rank(query, candidates)
    if not ranked:
        return None, False

    best = ranked[0]
    if best.score >= AUTO_ACCEPT:
        return best, False
    if best.score >= NEEDS_CONFIRMATION:
        return best, True
    return None, False
