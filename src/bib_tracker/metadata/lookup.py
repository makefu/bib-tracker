"""One-off lookups driven by a person waiting for the answer.

Unlike the enrichment queue, this runs inline: someone typed an ISBN and is
looking at a spinner, so it asks the providers that can answer by ISBN and
returns the first confident result.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderRecord, ProviderStatus

_LOGGER = logging.getLogger(__name__)

#: Order matters: the national library first for German titles, which is most
#: of what these libraries hold.
ISBN_PROVIDERS = ("dnb", "openlibrary", "googlebooks")


class ProviderSource(Protocol):
    """Just the part of the enrichment worker a lookup needs."""

    def usable_providers(self) -> dict[str, BaseProvider]: ...


async def lookup_isbn(source: ProviderSource, isbn: str) -> ProviderRecord | None:
    """Resolve an ISBN through whichever providers are usable."""
    usable = source.usable_providers()
    query = MediaQuery(media_class=MediaClass.BOOK, title="", isbn=isbn)

    for name in ISBN_PROVIDERS:
        provider = usable.get(name)
        if provider is None:
            continue
        try:
            record = await _ask(provider, query, isbn)
        except Exception as err:
            _LOGGER.info("ISBN lookup via %s failed: %s", name, err)
            continue
        if record is not None and record.status is ProviderStatus.OK and record.title:
            return record
    return None


async def _ask(provider: BaseProvider, query: MediaQuery, isbn: str) -> ProviderRecord | None:
    if provider.name == "openlibrary":
        direct = await provider.fetch(f"isbn:{isbn}")
        if direct is not None and direct.status is ProviderStatus.OK:
            return direct

    candidates = await provider.search(query)
    for candidate in candidates:
        # An ISBN search should return the edition asked for: take the first
        # that actually carries it, or the first result when none say.
        if candidate.isbn13 in (None, isbn):
            return await provider.fetch(candidate.external_id)
    return None
