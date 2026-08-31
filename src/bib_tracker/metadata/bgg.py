"""BoardGameGeek XML API.

The obvious source for board-game ratings, weight and player counts. As of
2026 it answers 401 to every anonymous request -- xmlapi and xmlapi2 alike,
regardless of user agent -- so it needs a credential and is therefore off
unless one is configured.
"""

from __future__ import annotations

from typing import ClassVar
from xml.etree import ElementTree

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus


class BoardGameGeekProvider(BaseProvider):
    name: ClassVar[str] = "bgg"
    supports: ClassVar[frozenset[MediaClass]] = frozenset({MediaClass.GAME})
    provides_rating: ClassVar[bool] = True
    requires_credentials: ClassVar[bool] = True
    DEFAULT_BASE_URL: ClassVar[str] = "https://boardgamegeek.com/xmlapi2"

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            return {}
        # Their documented scheme; a bare token also works for some accounts.
        return {"Authorization": f"Bearer {self.config.api_key}"}

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        if not self.available():
            return []

        response = await self.client.get(
            f"{self.base_url}/search",
            provider=self.name,
            params={"query": query.title, "type": "boardgame"},
            headers=self._headers(),
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return []

        return parse_search(response.text)

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        if not self.available():
            return self._unavailable(external_id)

        response = await self.client.get(
            f"{self.base_url}/thing",
            provider=self.name,
            params={"id": external_id, "stats": 1},
            headers=self._headers(),
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if response.status in (401, 403):
            return self._unavailable(external_id)
        if not response.ok:
            return ProviderRecord(provider=self.name, external_id=external_id, status=ProviderStatus.ERROR)

        return parse_thing(response.text, external_id)


def parse_search(xml: str) -> list[ProviderCandidate]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    candidates = []
    for item in root.findall("item"):
        name = item.find("name")
        year = item.find("yearpublished")
        candidates.append(
            ProviderCandidate(
                external_id=item.get("id", ""),
                title=(name.get("value") if name is not None else "") or "",
                year=int(year.get("value", 0)) if year is not None else None,
            )
        )
    return candidates


def parse_thing(xml: str, external_id: str) -> ProviderRecord:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return ProviderRecord(provider="bgg", external_id=external_id, status=ProviderStatus.ERROR)

    item = root.find("item")
    if item is None:
        return ProviderRecord(provider="bgg", external_id=external_id, status=ProviderStatus.NOT_FOUND)

    primary = next(
        (n for n in item.findall("name") if n.get("type") == "primary"),
        None,
    )
    ratings = item.find("statistics/ratings")
    average = ratings.find("average") if ratings is not None else None
    users = ratings.find("usersrated") if ratings is not None else None
    description = item.find("description")
    year = item.find("yearpublished")
    image = item.find("image")

    publishers = [link.get("value", "") for link in item.findall("link") if link.get("type") == "boardgamepublisher"]

    return ProviderRecord(
        provider="bgg",
        external_id=item.get("id", external_id),
        external_url=f"https://boardgamegeek.com/boardgame/{item.get('id', external_id)}",
        title=primary.get("value") if primary is not None else None,
        published_year=int(year.get("value", 0)) if year is not None else None,
        publisher=publishers[0] if publishers else None,
        description=(description.text or "").strip() if description is not None else None,
        # BGG rates out of 10. Kept on its own scale rather than rescaled, so
        # the interface can say "7.8/10" instead of implying a five-star score.
        rating_value=float(average.get("value", 0)) if average is not None else None,
        rating_scale=10.0 if average is not None else None,
        rating_count=int(users.get("value", 0)) if users is not None else None,
        cover_source_url=(image.text or "").strip() if image is not None else None,
        payload={"id": item.get("id")},
    )
