"""The metadata provider contract.

Providers differ wildly -- JSON, MARC XML, SPARQL -- so the protocol is
deliberately thin: search for candidates, fetch one record. Everything about
scoring, merging and storage lives outside them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..library.media_class import MediaClass


class ProviderStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"
    #: Configured but unusable without credentials. Distinct from an error so
    #: the interface can ask for a key rather than report a fault.
    NEEDS_CREDENTIALS = "needs_credentials"


class MatchMethod(StrEnum):
    ISBN = "isbn"
    EAN = "ean"
    TITLE_AUTHOR = "title_author"
    MANUAL = "manual"


@dataclass(frozen=True)
class MediaQuery:
    """What we know about a work before anyone looks it up."""

    media_class: MediaClass
    title: str
    author: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    published_year: int | None = None


@dataclass
class ProviderCandidate:
    external_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    isbn13: str | None = None
    #: Filled in by the matcher, never by the provider itself.
    score: float = 0.0
    #: A shop whose search answer already carries the full record (price,
    #: cover) fills this so the ladder needs no second request; catalogue
    #: providers leave it None and pay the fetch.
    record: ProviderRecord | None = None
    #: Loose per-tile extras a shop parsed but has no field for (price text).
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRecord:
    """One provider's answer about one work."""

    provider: str
    external_id: str
    status: ProviderStatus = ProviderStatus.OK
    external_url: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    isbn13: str | None = None
    published_year: int | None = None
    publisher: str | None = None
    page_count: int | None = None
    language: str | None = None
    description: str | None = None
    #: Kept on its own scale. Never averaged with another provider's: 7.8 out
    #: of 10 and 4.1 out of 5 are not the same number.
    rating_value: float | None = None
    rating_scale: float | None = None
    rating_count: int | None = None
    list_price_cents: int | None = None
    list_price_currency: str | None = None
    cover_source_url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    #: Filled in by the matcher after the fact, not by the provider.
    match_method: str | None = None
    match_confidence: float | None = None
    needs_confirmation: bool = False


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str | None = None
    api_key: str | None = None
    #: Whole `Cookie:` header value, for shop front-ends behind a login or a
    #: bot wall that a browser session cookie unlocks.
    cookie: str | None = None
    enabled: bool = True
    rate_limit_per_minute: int = 60


@runtime_checkable
class MetadataProvider(Protocol):
    name: ClassVar[str]
    #: Which media classes this provider knows anything about. The enrichment
    #: planner uses it to decide who to ask, which is the whole of the routing.
    supports: ClassVar[frozenset[MediaClass]]
    provides_price: ClassVar[bool]
    provides_rating: ClassVar[bool]
    #: True when the service refuses anonymous access.
    requires_credentials: ClassVar[bool]
    #: Shop front-ends serve price/cover, never bibliographic merge fields.
    shop: ClassVar[bool]

    def available(self) -> bool:
        """False when configured but missing a credential it cannot work without."""
        ...

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]: ...

    async def fetch(self, external_id: str) -> ProviderRecord | None: ...


class BaseProvider:
    """Shared plumbing. Providers override what they actually do."""

    name: ClassVar[str] = "base"
    supports: ClassVar[frozenset[MediaClass]] = frozenset()
    provides_price: ClassVar[bool] = False
    provides_rating: ClassVar[bool] = False
    requires_credentials: ClassVar[bool] = False
    #: Shop front-ends serve price/cover, never bibliographic merge fields.
    shop: ClassVar[bool] = False
    DEFAULT_BASE_URL: ClassVar[str] = ""

    def __init__(self, config: ProviderConfig, client: Any) -> None:
        self.config = config
        self.client = client
        self.base_url = (config.base_url or self.DEFAULT_BASE_URL).rstrip("/")

    def available(self) -> bool:
        if not self.config.enabled:
            return False
        return bool(self.config.api_key) if self.requires_credentials else True

    def supports_class(self, media_class: MediaClass) -> bool:
        return media_class in self.supports

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        return []

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        return None

    def _unavailable(self, external_id: str = "") -> ProviderRecord:
        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            status=ProviderStatus.NEEDS_CREDENTIALS,
        )
