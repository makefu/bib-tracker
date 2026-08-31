"""VLB (Verzeichnis Lieferbarer Bücher), via its REST API.

Since 2011 the VLB is, under the Börsenverein's Verkehrsordnung, the reference
database for the gebundener Ladenpreis, and the OLG Frankfurt has held that a
price recorded there takes precedence over a differing one on a publisher's own
site. So it is the most authoritative answer to "what did this cost".

It is not self-service: MVB grants developer access under an API agreement, and
production use needs a VLB subscription or a webshop contract. Without a token
this provider reports that it needs credentials rather than failing, and the
price ladder falls through to the DNB.

Unlike the other providers here, the response shape below is written from MVB's
documentation rather than from a recorded live response, because no token was
available. Treat the field names as the first thing to check if it misbehaves.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus

#: ONIX price types. 02 is the fixed retail price including tax, which is the
#: gebundener Ladenpreis; 04 is a recommended price where none is bound.
FIXED_RETAIL = "02"
RECOMMENDED_RETAIL = "04"
ACCEPTED_PRICE_TYPES = (FIXED_RETAIL, RECOMMENDED_RETAIL, "2", "4")


class VlbProvider(BaseProvider):
    name: ClassVar[str] = "vlb"
    supports: ClassVar[frozenset[MediaClass]] = frozenset({MediaClass.BOOK, MediaClass.AUDIOBOOK, MediaClass.MAGAZINE})
    provides_price: ClassVar[bool] = True
    requires_credentials: ClassVar[bool] = True
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.vlb.de/api/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Accept": "application/json"}

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        if not self.available() or not query.isbn:
            # Without an ISBN there is nothing to look a bound price up by.
            return []

        record = await self.fetch(query.isbn)
        if record is None or record.status is not ProviderStatus.OK:
            return []
        return [
            ProviderCandidate(
                external_id=record.external_id,
                title=record.title or "",
                authors=record.authors,
                year=record.published_year,
                isbn13=record.isbn13,
            )
        ]

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        if not self.available():
            return self._unavailable(external_id)

        isbn = external_id.replace("-", "").strip()
        response = await self.client.get(
            f"{self.base_url}/product/{isbn}",
            provider=self.name,
            headers=self._headers(),
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )
        if response.status in (401, 403):
            return self._unavailable(isbn)
        if response.status == 404:
            return ProviderRecord(provider=self.name, external_id=isbn, status=ProviderStatus.NOT_FOUND)
        if not response.ok:
            return ProviderRecord(provider=self.name, external_id=isbn, status=ProviderStatus.ERROR)

        try:
            payload = response.json()
        except ValueError:
            return ProviderRecord(provider=self.name, external_id=isbn, status=ProviderStatus.ERROR)

        return self._to_record(isbn, payload)

    def _to_record(self, isbn: str, payload: dict[str, Any]) -> ProviderRecord:
        cents, bound = extract_price(payload)
        return ProviderRecord(
            provider=self.name,
            external_id=str(payload.get("productId") or isbn),
            external_url=f"https://www.buchhandel.de/buch/{isbn}",
            title=payload.get("title") or payload.get("titles", [{}])[0].get("title"),
            authors=_authors(payload),
            isbn13=payload.get("isbn13") or isbn,
            published_year=_year(payload.get("publicationDate")),
            publisher=payload.get("publisher"),
            page_count=_int(payload.get("pages") or payload.get("extent")),
            list_price_cents=cents,
            list_price_currency="EUR" if cents is not None else None,
            payload={"price_is_bound": bound},
        )


def extract_price(payload: dict[str, Any]) -> tuple[int | None, bool]:
    """Pick the German retail price out of a VLB product.

    Returns the amount in cents and whether it is a bound price (as opposed to
    a mere recommendation, which the statistics should not treat as certain).
    """
    prices = payload.get("prices") or []
    if isinstance(prices, dict):
        prices = [prices]

    best: tuple[int, bool] | None = None
    for price in prices:
        if not isinstance(price, dict):
            continue
        if str(price.get("currencyCode") or price.get("currency") or "EUR").upper() != "EUR":
            continue
        country = str(price.get("countryCode") or price.get("country") or "DE").upper()
        if country and country != "DE":
            continue

        price_type = str(price.get("priceType") or price.get("priceTypeCode") or "")
        if price_type and price_type not in ACCEPTED_PRICE_TYPES:
            continue

        amount = price.get("priceAmount", price.get("amount"))
        if amount is None:
            continue
        try:
            cents = round(float(str(amount).replace(",", ".")) * 100)
        except ValueError:
            continue

        bound = price_type in (FIXED_RETAIL, "2")
        # A bound price outranks a recommendation.
        if best is None or (bound and not best[1]):
            best = (cents, bound)

    return best if best is not None else (None, False)


def _authors(payload: dict[str, Any]) -> list[str]:
    contributors = payload.get("contributors") or []
    names = []
    for entry in contributors:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or " ".join(part for part in (entry.get("firstName"), entry.get("lastName")) if part)
        if name:
            names.append(name)
    return names


def _year(value: Any) -> int | None:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
