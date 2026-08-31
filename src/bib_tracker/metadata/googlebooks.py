"""Google Books: descriptions, page counts, and the only free list price.

Anonymous access exists but is quota-limited per address and is exhausted
easily, so treat a key as the normal case and degrade honestly without one.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus


class GoogleBooksProvider(BaseProvider):
    name: ClassVar[str] = "googlebooks"
    supports: ClassVar[frozenset[MediaClass]] = frozenset({MediaClass.BOOK, MediaClass.AUDIOBOOK, MediaClass.MAGAZINE})
    provides_price: ClassVar[bool] = True
    provides_rating: ClassVar[bool] = True
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.googleapis.com/books/v1"

    #: List prices are country-specific; a euro price needs a German lookup.
    COUNTRY: ClassVar[str] = "DE"

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params = {"country": self.COUNTRY, **extra}
        if self.config.api_key:
            params["key"] = self.config.api_key
        return params

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        if query.isbn:
            expression = f"isbn:{query.isbn}"
        else:
            expression = f'intitle:"{query.title}"'
            if query.author:
                expression += f' inauthor:"{query.author}"'

        response = await self.client.get(
            f"{self.base_url}/volumes",
            provider=self.name,
            params=self._params({"q": expression, "maxResults": 5}),
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return []

        return [
            ProviderCandidate(
                external_id=item.get("id", ""),
                title=(item.get("volumeInfo") or {}).get("title", ""),
                authors=list((item.get("volumeInfo") or {}).get("authors") or []),
                year=_year((item.get("volumeInfo") or {}).get("publishedDate")),
                isbn13=_isbn13(item.get("volumeInfo") or {}),
            )
            for item in response.json().get("items") or []
            if item.get("id")
        ]

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        response = await self.client.get(
            f"{self.base_url}/volumes/{external_id}",
            provider=self.name,
            params=self._params({}),
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if response.status == 429:
            # Quota, not absence: retrying later is worthwhile, and saying
            # "not found" would poison the record with a wrong conclusion.
            return ProviderRecord(
                provider=self.name,
                external_id=external_id,
                status=ProviderStatus.ERROR,
                payload={"error": "quota exceeded"},
            )
        if response.status == 404:
            return ProviderRecord(provider=self.name, external_id=external_id, status=ProviderStatus.NOT_FOUND)
        if not response.ok:
            return ProviderRecord(provider=self.name, external_id=external_id, status=ProviderStatus.ERROR)

        return self._to_record(response.json())

    def _to_record(self, item: dict[str, Any]) -> ProviderRecord:
        info = item.get("volumeInfo") or {}
        sale = item.get("saleInfo") or {}
        images = info.get("imageLinks") or {}
        price = sale.get("listPrice") or {}

        return ProviderRecord(
            provider=self.name,
            external_id=item.get("id", ""),
            external_url=info.get("infoLink"),
            title=info.get("title"),
            authors=list(info.get("authors") or []),
            isbn13=_isbn13(info),
            published_year=_year(info.get("publishedDate")),
            publisher=info.get("publisher"),
            page_count=info.get("pageCount"),
            language=info.get("language"),
            description=info.get("description"),
            rating_value=info.get("averageRating"),
            rating_scale=5.0 if info.get("averageRating") is not None else None,
            rating_count=info.get("ratingsCount"),
            list_price_cents=_cents(price.get("amount")),
            list_price_currency=price.get("currencyCode"),
            cover_source_url=_https(images.get("large") or images.get("thumbnail")),
            payload={"volumeInfo": info, "saleInfo": sale},
        )


def _isbn13(info: dict[str, Any]) -> str | None:
    for identifier in info.get("industryIdentifiers") or []:
        if identifier.get("type") == "ISBN_13":
            return str(identifier.get("identifier"))
    return None


def _year(value: Any) -> int | None:
    if not value:
        return None
    text = str(value)[:4]
    return int(text) if text.isdigit() else None


def _cents(amount: Any) -> int | None:
    return None if amount is None else round(float(amount) * 100)


def _https(url: str | None) -> str | None:
    """Google still hands out http:// thumbnail links."""
    return url.replace("http://", "https://") if url else None
