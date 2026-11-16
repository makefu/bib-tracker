"""Ask every source at once and let a person pick.

The matcher decides alone: above a threshold it adopts a provider's answer,
below it discards it. Both are wrong when the work is real but the data is
thin, which is where a hand-entered row usually sits. This module runs the
same providers without the threshold and returns what they said, so the pick
is made by someone who can see the cover, and the row lands in
``metadata_records`` with ``confirmed = 1`` — the same shape the worker's
auto-match produces, so the merge and the ladders need no new code path.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from ..db.connection import Database
from ..library.media_class import MediaClass
from .base import MatchMethod, MediaQuery, ProviderRecord, ProviderStatus
from .http import CachedClient
from .matcher import rank
from .worker import apply_records, build_providers, store_record

#: Cards per provider in the results list. A shop's search page returns
#: dozens of editions; the picker is meant to be read, not scrolled.
MAX_CARDS_PER_PROVIDER = 4

#: A loose ISBN entry: only digits, spaces and hyphens, ten to seventeen
#: characters (a hyphenated ISBN-13 runs to seventeen).
_LOOSE_ISBN = re.compile(r"^[\d -]{9,16}[\dXx]$")

__all__ = [
    "MAX_CARDS_PER_PROVIDER",
    "apply_choice",
    "build_for",
    "parse_query",
    "provider_names",
    "search_cards",
    "usable_names",
]


def provider_names(settings: Settings) -> list[str]:
    """Every configured source, catalogues first, then the shops.

    The union of the three lists: the dialog answers title, price and cover in
    one pass, so a source configured for any of them is worth asking.
    """
    order = list(dict.fromkeys([*settings.metadata_providers, *settings.price_providers, *settings.image_providers]))
    return [name for name in order if name != "library"]


def build_for(settings: Settings, client: Any, names: list[str], media_class: MediaClass) -> dict[str, Any]:
    """The usable sources that know anything about this kind of media.

    A board-game database asked about a novel returns noise it scored as a
    match, so the media class gates who gets asked here exactly as it does in
    the worker's enqueue step.
    """
    providers = build_providers(settings, client, names)
    return {name: p for name, p in providers.items() if p.supports_class(media_class)}


def usable_names(settings: Settings, media_class: str) -> list[str]:
    """Which sources the dialog will actually ask, for the button's disabled state.

    Built with no client: ``available()`` reads the config alone, and the page
    must not open an HTTP client to decide whether to draw a button.
    """
    return list(build_for(settings, None, provider_names(settings), _class(media_class)))


def as_isbn13(text: str) -> str | None:
    """Digits of a loosely typed ISBN, or None. Check digits are not verified.

    Someone pasting an ISBN from a library page has hyphens in it; the shops'
    search endpoints do not accept them.
    """
    stripped = text.strip()
    if not _LOOSE_ISBN.match(stripped):
        return None
    digits = re.sub(r"[ -]", "", stripped)
    if len(digits) == 13 and digits.isdigit():
        return digits
    if len(digits) == 10 and digits[:9].isdigit():
        return _isbn10_to_13(digits[:9] + digits[9].upper())
    return None


def _isbn10_to_13(digits: str) -> str:
    body = "978" + digits[:9]
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(body))
    return body + str((10 - total % 10) % 10)


def parse_query(media_class: str, text: str, media_isbn: str | None = None) -> MediaQuery:
    """Turn one search box into a provider query.

    An ISBN-looking entry is searched as an ISBN *and* kept as the title: a
    shop's ISBN search that finds nothing falls back to the title, and the
    title fallback has to say something. A free-text entry never becomes an
    ISBN, because a wrong stored ISBN distorts every later score.
    """
    stripped = text.strip()
    isbn = as_isbn13(stripped)
    if isbn is not None:
        # The text stays the title: a shop whose ISBN search finds nothing
        # falls back to the title, and that fallback has to say something.
        # A loose entry is its own work, so it carries no stored ISBN.
        return MediaQuery(media_class=_class(media_class), title=stripped, author=None, isbn=isbn)
    return MediaQuery(media_class=_class(media_class), title=stripped, author=None, isbn=media_isbn)


def _class(value: str) -> MediaClass:
    try:
        return MediaClass(value)
    except ValueError:
        return MediaClass.OTHER


async def search_cards(provider: Any, query: MediaQuery) -> list[dict[str, Any]]:
    """What one provider says about a query, as picker cards.

    No threshold: every tile the provider returns is shown, ranked. The
    matcher's job here is ordering, not deciding — a person is doing the
    deciding, which is the entire point of the dialog.
    """
    candidates = await provider.search(query)
    ranked = rank(query, candidates)
    cards: list[dict[str, Any]] = []
    for candidate in ranked[:MAX_CARDS_PER_PROVIDER]:
        record = candidate.record
        if record is None:
            # The tile carries title and price but not the cover or the
            # publisher; one fetch per shown card is the price of a card that
            # can actually be compared.
            record = await provider.fetch(candidate.external_id)
        cards.append(_card(provider, candidate.title, candidate, record))
    return cards


def _card(provider: Any, fallback_title: str, candidate: Any, record: ProviderRecord | None) -> dict[str, Any]:
    title = (record.title if record and record.title else candidate.title) or fallback_title
    authors = (record.authors if record and record.authors else candidate.authors) or []
    return {
        "provider": provider.name,
        "external_id": candidate.external_id,
        "external_url": (record.external_url if record else None) or candidate.payload.get("url"),
        "title": title,
        "authors": ", ".join(authors[:3]),
        "year": record.published_year if record else candidate.year,
        "publisher": record.publisher if record else None,
        "isbn13": (record.isbn13 if record else None) or candidate.isbn13,
        "price_cents": record.list_price_cents if record else None,
        "price_currency": record.list_price_currency if record else None,
        "cover_url": record.cover_source_url if record else None,
        "score": candidate.score,
    }


async def apply_choice(
    db: Database, settings: Settings, http: CachedClient, media_id: int, provider_name: str, external_id: str
) -> int:
    """Store a hand-picked record as confirmed and merge it in.

    The re-fetch is what makes the click safe: the card was built from a
    search tile, and the tile is not the record. Returns the id the work lives
    under afterwards, because adopting an ISBN moves the row.
    """
    provider = build_providers(settings, http, [provider_name]).get(provider_name)
    if provider is None:
        raise RuntimeError(f"provider {provider_name} ist nicht verfügbar")
    record = await provider.fetch(external_id)
    if record is None or record.status is not ProviderStatus.OK:
        raise LookupError(record.status.value if record else "not_found")

    # A human looked: the matcher's confidence no longer applies.
    record.match_method = MatchMethod.MANUAL
    record.match_confidence = 1.0
    record.needs_confirmation = False
    await store_record(db, media_id, record, confirmed=True)
    return await apply_records(db, settings, http, media_id)
