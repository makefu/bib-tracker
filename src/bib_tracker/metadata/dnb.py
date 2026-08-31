"""Deutsche Nationalbibliothek, over SRU.

The best coverage for German-language titles, which is most of what these
libraries hold and much of what Open Library has never heard of. Free, no key.
Returns MARC21-XML, so the field numbers below are the interface.
"""

from __future__ import annotations

import re
from typing import ClassVar
from xml.etree import ElementTree

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus

MARC_NS = {"marc": "http://www.loc.gov/MARC21/slim", "srw": "http://www.loc.gov/zing/srw/"}


class DnbProvider(BaseProvider):
    name: ClassVar[str] = "dnb"
    supports: ClassVar[frozenset[MediaClass]] = frozenset({MediaClass.BOOK, MediaClass.AUDIOBOOK, MediaClass.MAGAZINE})
    DEFAULT_BASE_URL: ClassVar[str] = "https://services.dnb.de/sru/dnb"

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        if query.isbn:
            expression = f"NUM={query.isbn}"
        else:
            expression = f"WOE={query.title}"
            if query.author:
                expression += f" and WOE={query.author}"

        records = await self._search(expression, limit=5)
        return [
            ProviderCandidate(
                external_id=record.external_id,
                title=record.title or "",
                authors=record.authors,
                year=record.published_year,
                isbn13=record.isbn13,
            )
            for record in records
        ]

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        records = await self._search(f"NID={external_id}", limit=1)
        if records:
            return records[0]
        return ProviderRecord(provider=self.name, external_id=external_id, status=ProviderStatus.NOT_FOUND)

    async def _search(self, expression: str, limit: int) -> list[ProviderRecord]:
        response = await self.client.get(
            self.base_url,
            provider=self.name,
            params={
                "version": "1.1",
                "operation": "searchRetrieve",
                "query": expression,
                "recordSchema": "MARC21-xml",
                "maximumRecords": limit,
            },
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if not response.ok:
            return []
        return parse_sru(response.text, provider=self.name)


def parse_sru(xml: str, provider: str = "dnb") -> list[ProviderRecord]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    records: list[ProviderRecord] = []
    for marc in root.iter(f"{{{MARC_NS['marc']}}}record"):
        record = _from_marc(marc, provider)
        if record is not None:
            records.append(record)
    return records


def _from_marc(marc: ElementTree.Element, provider: str) -> ProviderRecord | None:
    identifier = _controlfield(marc, "001")
    if identifier is None:
        return None

    title = _subfield(marc, "245", "a")
    subtitle = _subfield(marc, "245", "b")
    if title and subtitle:
        title = f"{title}: {subtitle}"

    return ProviderRecord(
        provider=provider,
        external_id=identifier,
        external_url=f"https://d-nb.info/{identifier}",
        title=_clean(title),
        authors=_authors(marc),
        isbn13=_isbn13(marc),
        published_year=_year(_subfield(marc, "264", "c") or _subfield(marc, "260", "c")),
        publisher=_clean(_subfield(marc, "264", "b") or _subfield(marc, "260", "b")),
        page_count=_pages(_subfield(marc, "300", "a")),
        language=_subfield(marc, "041", "a"),
        payload={"id": identifier},
    )


def _authors(marc: ElementTree.Element) -> list[str]:
    """Main and added entries, in order, without repeats.

    MARC often lists the same person under both 100 and 700.
    """
    found: list[str] = []
    for tag in ("100", "700"):
        name = _clean(_subfield(marc, tag, "a"))
        if name and name not in found:
            found.append(name)
    return found


def _controlfield(marc: ElementTree.Element, tag: str) -> str | None:
    for field in marc.iter(f"{{{MARC_NS['marc']}}}controlfield"):
        if field.get("tag") == tag:
            return (field.text or "").strip() or None
    return None


def _subfield(marc: ElementTree.Element, tag: str, code: str) -> str | None:
    for field in marc.iter(f"{{{MARC_NS['marc']}}}datafield"):
        if field.get("tag") != tag:
            continue
        for sub in field.iter(f"{{{MARC_NS['marc']}}}subfield"):
            if sub.get("code") == code:
                text = (sub.text or "").strip()
                if text:
                    return text
    return None


def _isbn13(marc: ElementTree.Element) -> str | None:
    for field in marc.iter(f"{{{MARC_NS['marc']}}}datafield"):
        if field.get("tag") != "020":
            continue
        for sub in field.iter(f"{{{MARC_NS['marc']}}}subfield"):
            if sub.get("code") != "a":
                continue
            digits = re.sub(r"[^0-9Xx]", "", sub.text or "")
            if len(digits) == 13 and digits.startswith(("978", "979")):
                return digits
    return None


#: MARC brackets non-filing characters with control codes, so a title arrives
#: as "\u0098Die\u009c unendliche Geschichte". They are not part of the title.
_NON_FILING = str.maketrans("", "", "\u0098\u009c\u0088\u0089\u00ac")


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    # MARC also ends fields with ISBD punctuation, noise outside a catalogue.
    return value.translate(_NON_FILING).rstrip(" /:;,").strip() or None


def _year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(1[5-9]\d{2}|20\d{2})", value)
    return int(match.group(1)) if match else None


def _pages(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)\s*(?:S\.|Seiten|p\.|pages)", value)
    return int(match.group(1)) if match else None
