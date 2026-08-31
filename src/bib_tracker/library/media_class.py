"""Map a library's own media-type vocabulary onto our own.

Deliberately not upstream in ha_stadtbibliothek: the vocabulary is per-library
and what a consumer wants from it differs. Home Assistant wants the raw string
to display; we want an enum to route metadata lookups with.
"""

from __future__ import annotations

import logging
from enum import StrEnum

_LOGGER = logging.getLogger(__name__)


class MediaClass(StrEnum):
    BOOK = "book"
    AUDIOBOOK = "audiobook"
    MUSIC = "music"
    MOVIE = "movie"
    GAME = "game"
    MAGAZINE = "magazine"
    OTHER = "other"


#: Raw media_type strings as the OPACs report them, lowercased.
_MEDIA_TYPES: dict[str, MediaClass] = {
    "buch": MediaClass.BOOK,
    "sachbuch": MediaClass.BOOK,
    "roman": MediaClass.BOOK,
    "kinderbuch": MediaClass.BOOK,
    "comic": MediaClass.BOOK,
    "hörbuch": MediaClass.AUDIOBOOK,
    "hoerbuch": MediaClass.AUDIOBOOK,
    "hörspiel": MediaClass.AUDIOBOOK,
    "cd-hörbuch": MediaClass.AUDIOBOOK,
    "tonie": MediaClass.AUDIOBOOK,
    "cd": MediaClass.MUSIC,
    "musik-cd": MediaClass.MUSIC,
    "musik": MediaClass.MUSIC,
    "dvd": MediaClass.MOVIE,
    "blu-ray": MediaClass.MOVIE,
    "film": MediaClass.MOVIE,
    "video": MediaClass.MOVIE,
    "konventionelles spiel": MediaClass.GAME,
    "spiel": MediaClass.GAME,
    "brettspiel": MediaClass.GAME,
    "gesellschaftsspiel": MediaClass.GAME,
    "konsolenspiel": MediaClass.GAME,
    "zeitschrift": MediaClass.MAGAZINE,
    "zeitung": MediaClass.MAGAZINE,
}

#: Stuttgart encodes the type in the call number, which is often the only
#: signal: its loan table omits the media type prefix entirely for books.
_CALL_NUMBER_PREFIXES: tuple[tuple[str, MediaClass], ...] = (
    ("s-spiel", MediaClass.GAME),
    ("m-cd", MediaClass.MUSIC),
    ("m-dvd", MediaClass.MOVIE),
    ("m-bd", MediaClass.MOVIE),
)

_unmapped_seen: set[str] = set()


def classify(
    media_type: str | None,
    call_number: str | None = None,
    isbn: str | None = None,
    overrides: dict[str, str] | None = None,
) -> MediaClass:
    """Return the media class, taking the first signal that resolves."""
    raw = (media_type or "").strip().lower()

    if raw and overrides and raw in {k.lower() for k in overrides}:
        lowered = {k.lower(): v for k, v in overrides.items()}
        return MediaClass(lowered[raw])

    if raw in _MEDIA_TYPES:
        return _MEDIA_TYPES[raw]

    call = (call_number or "").strip().lower()
    for prefix, media_class in _CALL_NUMBER_PREFIXES:
        if call.startswith(prefix):
            return media_class

    if raw:
        _note_unmapped(raw)

    if isbn:
        return MediaClass.BOOK
    if not raw:
        # No type and no call-number hint: Stuttgart books look exactly like
        # this, and books are by far the common case.
        return MediaClass.BOOK
    return MediaClass.OTHER


def _note_unmapped(raw: str) -> None:
    if raw not in _unmapped_seen:
        _unmapped_seen.add(raw)
        _LOGGER.info("Unmapped library media type %r; add it to the media class map", raw)


def unmapped_media_types() -> list[str]:
    """Raw strings seen but not recognised, for surfacing in the interface."""
    return sorted(_unmapped_seen)
