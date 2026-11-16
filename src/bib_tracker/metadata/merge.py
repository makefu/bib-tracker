"""Combining several providers' answers into one record.

Per-field precedence rather than "last writer wins", declared in one table so
it can be read and changed without hunting through code. Ratings are never
merged: they stay per provider, because 7.8 out of 10 and 4.1 out of 5 are not
the same claim and averaging them produces a number nobody stated.
"""

from __future__ import annotations

from typing import Any

from .base import ProviderRecord, ProviderStatus

#: Shop front-ends, appended to every field's tuple rather than listed in it:
#: the worker never queues a shop for the merge, so a shop record only exists
#: once a person confirmed it in the identify dialog. It may fill a gap no
#: catalogue stated, but it must never outrank one that did.
SHOPS: tuple[str, ...] = ("buchkatalog", "thalia", "amazon", "buch7", "lehmanns", "ebookde")

#: Fields that identify the work itself, taken from a shop only as a last
#: resort and never over a catalogue: a shop's title or publisher is
#: marketing text for its own edition. The ISBN is exempt — it is a factual
#: identifier, and a pick is often the only way a work gets one at all.
BIBLIOGRAPHIC: frozenset[str] = frozenset(("title", "publisher", "language", "published_year", "page_count"))

#: First provider in the tuple that has a value for the field wins; the shop
#: tail only ever fills a gap.
FIELD_PRECEDENCE: dict[str, tuple[str, ...]] = {
    # The national library is authoritative on German bibliographic data.
    "isbn13": ("dnb", "googlebooks", "openlibrary"),
    "title": ("dnb", "googlebooks", "openlibrary", "wikidata", "bgg"),
    "publisher": ("dnb", "googlebooks", "openlibrary", "bgg", "wikidata"),
    "language": ("dnb", "googlebooks", "openlibrary"),
    # Google normalises editions better than a catalogue record does.
    "published_year": ("googlebooks", "openlibrary", "dnb", "bgg", "wikidata"),
    "page_count": ("googlebooks", "openlibrary", "dnb"),
    "description": ("googlebooks", "openlibrary", "bgg", "wikidata"),
    "cover_source_url": ("openlibrary", "googlebooks", "bgg", "wikidata"),
    # Order mirrors the default price preference: the VLB is the reference
    # database for the bound retail price, the DNB has it as catalogued, and
    # Google Books rarely knows German titles at all. The effective price is
    # chosen by pricing.all_found_prices(), which honours the user's
    # configured order across catalogue and shop sources alike; this is only
    # the fallback for a merged record.
    "list_price_cents": ("vlb", "dnb", "googlebooks"),
    "list_price_currency": ("vlb", "dnb", "googlebooks"),
}
for _field, _providers in FIELD_PRECEDENCE.items():
    FIELD_PRECEDENCE[_field] = _providers + SHOPS


MERGED_FIELDS = tuple(FIELD_PRECEDENCE)


def merge_records(records: list[ProviderRecord]) -> dict[str, Any]:
    """Fold provider answers into the fields a media row holds."""
    usable = {r.provider: r for r in records if r.status is ProviderStatus.OK}

    merged: dict[str, Any] = {}
    for field, providers in FIELD_PRECEDENCE.items():
        for provider in providers:
            record = usable.get(provider)
            if record is None:
                continue
            # The shop tail exists for cover and price only: a confirmed shop
            # record answers gaps, and its title, author or publisher must
            # never become the authority over a catalogue that stated one.
            if provider in SHOPS and field in BIBLIOGRAPHIC:
                continue
            value = getattr(record, field, None)
            if value not in (None, "", []):
                merged[field] = value
                break

    authors = _authors(usable)
    if authors:
        merged["author"] = authors
    return merged


def _authors(usable: dict[str, ProviderRecord]) -> str | None:
    for provider in FIELD_PRECEDENCE["title"]:
        record = usable.get(provider)
        if record and record.authors:
            return ", ".join(record.authors[:3])
    return None


def ratings(records: list[ProviderRecord]) -> list[dict[str, Any]]:
    """Every provider's rating, on its own scale, for side-by-side display."""
    return [
        {
            "provider": record.provider,
            "value": record.rating_value,
            "scale": record.rating_scale,
            "count": record.rating_count,
        }
        for record in records
        if record.status is ProviderStatus.OK and record.rating_value is not None
    ]


def consensus_rating(records: list[ProviderRecord]) -> float | None:
    """A single 0-1 figure, for sorting only.

    Deliberately not stored: it is a convenience for ordering a list, not a
    rating anyone gave.
    """
    weighted: list[tuple[float, float]] = []
    for record in records:
        if record.status is not ProviderStatus.OK or record.rating_value is None or not record.rating_scale:
            continue
        weight = float(record.rating_count or 1)
        weighted.append((record.rating_value / record.rating_scale, weight))

    if not weighted:
        return None
    total = sum(weight for _, weight in weighted)
    return sum(value * weight for value, weight in weighted) / total
