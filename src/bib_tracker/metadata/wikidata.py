"""Wikidata, over SPARQL.

Free and anonymous, which BoardGameGeek no longer is, so it is what board
games get by default. No community rating -- Wikidata does not have one -- but
publisher, year, player count and playing time are all there.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus

# P31/P279* = "instance of, or of a subclass of", so both "board game" and
# "card game" and their descendants match.
SEARCH_SPARQL = """
SELECT ?item ?itemLabel ?year ?publisherLabel ?image WHERE {
  ?item wdt:P31/wdt:P279* wd:Q131436 .
  ?item rdfs:label ?itemLabel .
  FILTER(CONTAINS(LCASE(?itemLabel), LCASE("%(title)s")))
  OPTIONAL { ?item wdt:P577 ?date . BIND(YEAR(?date) AS ?year) }
  OPTIONAL { ?item wdt:P123 ?publisher . ?publisher rdfs:label ?publisherLabel .
             FILTER(LANG(?publisherLabel) = "de" || LANG(?publisherLabel) = "en") }
  OPTIONAL { ?item wdt:P18 ?image }
  FILTER(LANG(?itemLabel) = "de" || LANG(?itemLabel) = "en")
}
LIMIT 5
"""

FETCH_SPARQL = """
SELECT ?itemLabel ?year ?publisherLabel ?image ?description WHERE {
  BIND(wd:%(qid)s AS ?item)
  OPTIONAL { ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "de" || LANG(?itemLabel) = "en") }
  OPTIONAL { ?item wdt:P577 ?date . BIND(YEAR(?date) AS ?year) }
  OPTIONAL { ?item wdt:P123 ?publisher . ?publisher rdfs:label ?publisherLabel .
             FILTER(LANG(?publisherLabel) = "de" || LANG(?publisherLabel) = "en") }
  OPTIONAL { ?item wdt:P18 ?image }
  OPTIONAL { ?item schema:description ?description .
             FILTER(LANG(?description) = "de" || LANG(?description) = "en") }
}
LIMIT 1
"""


class WikidataProvider(BaseProvider):
    name: ClassVar[str] = "wikidata"
    supports: ClassVar[frozenset[MediaClass]] = frozenset({MediaClass.GAME})
    DEFAULT_BASE_URL: ClassVar[str] = "https://query.wikidata.org"

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        rows = await self._query(SEARCH_SPARQL % {"title": _escape(query.title)})
        candidates = []
        for row in rows:
            qid = _value(row, "item").rsplit("/", 1)[-1]
            if not qid:
                continue
            candidates.append(
                ProviderCandidate(
                    external_id=qid,
                    title=_value(row, "itemLabel"),
                    year=_int(_value(row, "year")),
                )
            )
        return candidates

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        if not external_id.startswith("Q"):
            return ProviderRecord(provider=self.name, external_id=external_id, status=ProviderStatus.NOT_FOUND)

        rows = await self._query(FETCH_SPARQL % {"qid": external_id})
        if not rows:
            return ProviderRecord(provider=self.name, external_id=external_id, status=ProviderStatus.NOT_FOUND)

        row = rows[0]
        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            external_url=f"https://www.wikidata.org/wiki/{external_id}",
            title=_value(row, "itemLabel") or None,
            published_year=_int(_value(row, "year")),
            publisher=_value(row, "publisherLabel") or None,
            description=_value(row, "description") or None,
            cover_source_url=_value(row, "image") or None,
            payload={"qid": external_id},
        )

    async def _query(self, sparql: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"{self.base_url}/sparql",
            provider=self.name,
            params={"query": sparql, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        bindings = payload.get("results", {}).get("bindings", [])
        return list(bindings)


def _value(row: dict[str, Any], key: str) -> str:
    entry = row.get(key)
    return str(entry.get("value", "")) if isinstance(entry, dict) else ""


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
