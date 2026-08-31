"""Metadata providers.

Adding a source is one module and one line in PROVIDER_FACTORIES; nothing else
in the application knows which providers exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import (
    BaseProvider,
    MatchMethod,
    MediaQuery,
    MetadataProvider,
    ProviderCandidate,
    ProviderConfig,
    ProviderRecord,
    ProviderStatus,
)
from .bgg import BoardGameGeekProvider
from .dnb import DnbProvider
from .googlebooks import GoogleBooksProvider
from .openlibrary import OpenLibraryProvider
from .wikidata import WikidataProvider

PROVIDER_FACTORIES: dict[str, Callable[[ProviderConfig, Any], BaseProvider]] = {
    OpenLibraryProvider.name: OpenLibraryProvider,
    GoogleBooksProvider.name: GoogleBooksProvider,
    DnbProvider.name: DnbProvider,
    BoardGameGeekProvider.name: BoardGameGeekProvider,
    WikidataProvider.name: WikidataProvider,
}


def available_providers() -> list[str]:
    return sorted(PROVIDER_FACTORIES)


def build_provider(config: ProviderConfig, client: Any) -> BaseProvider:
    try:
        factory = PROVIDER_FACTORIES[config.name]
    except KeyError:
        raise KeyError(f"Unknown metadata provider {config.name!r}; known: {available_providers()}") from None
    return factory(config, client)


__all__ = [
    "PROVIDER_FACTORIES",
    "BaseProvider",
    "BoardGameGeekProvider",
    "DnbProvider",
    "GoogleBooksProvider",
    "MatchMethod",
    "MediaQuery",
    "MetadataProvider",
    "OpenLibraryProvider",
    "ProviderCandidate",
    "ProviderConfig",
    "ProviderRecord",
    "ProviderStatus",
    "WikidataProvider",
    "available_providers",
    "build_provider",
]
