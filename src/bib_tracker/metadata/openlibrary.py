"""Open Library: covers, bibliographic data and community ratings. Free, no key."""

from __future__ import annotations

from typing import Any, ClassVar

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus

SEARCH_FIELDS = (
    "key,title,author_name,first_publish_year,isbn,cover_i,ratings_average,ratings_count,number_of_pages_median"
)


class OpenLibraryProvider(BaseProvider):
    name: ClassVar[str] = "openlibrary"
    supports: ClassVar[frozenset[MediaClass]] = frozenset({MediaClass.BOOK, MediaClass.AUDIOBOOK})
    provides_rating: ClassVar[bool] = True
    DEFAULT_BASE_URL: ClassVar[str] = "https://openlibrary.org"

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        params: dict[str, Any] = {"limit": 5, "fields": SEARCH_FIELDS}
        if query.isbn:
            params["q"] = f"isbn:{query.isbn}"
        else:
            params["title"] = query.title
            if query.author:
                params["author"] = query.author

        response = await self.client.get(
            f"{self.base_url}/search.json",
            provider=self.name,
            params=params,
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return []

        docs = response.json().get("docs") or []
        return [
            ProviderCandidate(
                external_id=doc.get("key", ""),
                title=doc.get("title", ""),
                authors=list(doc.get("author_name") or []),
                year=doc.get("first_publish_year"),
                isbn13=_first_isbn13(doc.get("isbn")),
            )
            for doc in docs
            if doc.get("key")
        ]

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        """Fetch by ISBN (``isbn:…``) or by work key (``/works/OL…``)."""
        if external_id.startswith("isbn:"):
            return await self._fetch_by_isbn(external_id.removeprefix("isbn:"))
        return await self._fetch_work(external_id)

    async def _fetch_by_isbn(self, isbn: str) -> ProviderRecord | None:
        response = await self.client.get(
            f"{self.base_url}/api/books",
            provider=self.name,
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return ProviderRecord(provider=self.name, external_id=isbn, status=ProviderStatus.ERROR)

        payload = response.json()
        record = payload.get(f"ISBN:{isbn}")
        if not record:
            return ProviderRecord(provider=self.name, external_id=isbn, status=ProviderStatus.NOT_FOUND)

        identifiers = record.get("identifiers") or {}
        cover = record.get("cover") or {}
        return ProviderRecord(
            provider=self.name,
            external_id=record.get("key") or f"isbn:{isbn}",
            external_url=record.get("url"),
            title=record.get("title"),
            authors=[a.get("name", "") for a in record.get("authors") or []],
            isbn13=_first(identifiers.get("isbn_13")) or isbn,
            published_year=_year(record.get("publish_date")),
            publisher=_first_name(record.get("publishers")),
            page_count=record.get("number_of_pages"),
            cover_source_url=cover.get("large") or cover.get("medium") or cover.get("small"),
            payload=record,
        )

    async def _fetch_work(self, work_key: str) -> ProviderRecord | None:
        response = await self.client.get(
            f"{self.base_url}/search.json",
            provider=self.name,
            params={"q": f"key:{work_key}", "fields": SEARCH_FIELDS, "limit": 1},
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return ProviderRecord(provider=self.name, external_id=work_key, status=ProviderStatus.ERROR)

        docs = response.json().get("docs") or []
        if not docs:
            return ProviderRecord(provider=self.name, external_id=work_key, status=ProviderStatus.NOT_FOUND)

        doc = docs[0]
        cover_id = doc.get("cover_i")
        return ProviderRecord(
            provider=self.name,
            external_id=work_key,
            external_url=f"{self.base_url}{work_key}",
            title=doc.get("title"),
            authors=list(doc.get("author_name") or []),
            isbn13=_first_isbn13(doc.get("isbn")),
            published_year=doc.get("first_publish_year"),
            page_count=doc.get("number_of_pages_median"),
            rating_value=doc.get("ratings_average"),
            rating_scale=5.0 if doc.get("ratings_average") is not None else None,
            rating_count=doc.get("ratings_count"),
            cover_source_url=(f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None),
            payload=doc,
        )


def _first(values: Any) -> str | None:
    return str(values[0]) if isinstance(values, list) and values else None


def _first_name(values: Any) -> str | None:
    if isinstance(values, list) and values:
        entry = values[0]
        return entry.get("name") if isinstance(entry, dict) else str(entry)
    return None


def _first_isbn13(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    for value in values:
        text = str(value).replace("-", "")
        if len(text) == 13 and text.startswith(("978", "979")):
            return text
    return None


def _year(value: Any) -> int | None:
    if not value:
        return None
    import re

    match = re.search(r"(1[5-9]\d{2}|20\d{2})", str(value))
    return int(match.group(1)) if match else None
