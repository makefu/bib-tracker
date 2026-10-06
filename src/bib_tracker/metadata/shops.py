"""Shop front-ends: Buchkatalog.de, Thalia, Amazon.de, buch7.de, Lehmanns.de, eBook.de.

These are the retail pages, not catalogue databases. They answer what a copy
costs at a German shop today and what its cover looks like — they are never
asked for publisher or page count, which is why the enrichment queue skips
them (`shop = True`) and only the price/cover ladders walk them.

Every request shape and parse target in this module was captured from the
live site; the captures live in `tests/fixtures/providers/` and the tests
drive the parsers with them. A shop front-end that is not answered exactly
the way it was recorded will quietly 403 or return an empty page, so change
these selectors together with a refreshed fixture.
"""

from __future__ import annotations

import html as html_module
import json
import re
from typing import Any, ClassVar

from ..library.media_class import MediaClass
from .base import BaseProvider, MediaQuery, ProviderCandidate, ProviderRecord
from .pricing import parse_price_cents

#: Shop front-ends answer 403 to anything that does not look like a typed-in
#: browser request; with only a User-Agent, Thalia 403s even on its homepage.
SHOP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Thalia-family interstitial title (Osiander and Weltbild serve the same page).
_CHALLENGE_MARKER = "Sicherheits-Check"

_ALL_CLASSES: frozenset[MediaClass] = frozenset(
    {
        MediaClass.BOOK,
        MediaClass.AUDIOBOOK,
        MediaClass.MUSIC,
        MediaClass.MOVIE,
        MediaClass.GAME,
        MediaClass.MAGAZINE,
        MediaClass.OTHER,
    }
)


class ShopBlocked(Exception):
    """The shop answered with its bot wall rather than its catalogue.

    Distinct from "this shop does not stock the work": a 403 or a challenge
    page says nothing about the work, and the ladder records it as `blocked`
    so the UI can mark the source instead of pretending it came up empty.
    """


def _text(fragment: str) -> str:
    """Visible text of an HTML fragment, entities resolved."""
    return " ".join(html_module.unescape(re.sub(r"<[^>]*>", " ", fragment)).split())


def _first(pattern: str, subject: str) -> str | None:
    match = re.search(pattern, subject, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _ld_nodes(page: str) -> list[dict[str, Any]]:
    """Every JSON-LD object on a page, through whatever wrapper Next.js wraps it in."""
    nodes: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("@graph"), list):
                for child in value["@graph"]:
                    add(child)
            else:
                nodes.append(value)
        elif isinstance(value, list):
            for child in value:
                add(child)

    for match in re.finditer(
        r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
        page,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            add(json.loads(html_module.unescape(match.group(1))))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return nodes


def _product_node(page: str) -> dict[str, Any] | None:
    """The Product/Book node; a page may carry Book and Product for one work."""
    best: dict[str, Any] | None = None
    for node in _ld_nodes(page):
        kind = node.get("@type")
        kinds = {kind} if isinstance(kind, str) else set(kind) if isinstance(kind, list) else set()
        if not kinds & {"Product", "Book"}:
            continue
        if best is None or "Product" in kinds:
            best = node
    return best


def _offer_field(node: dict[str, Any], key: str) -> Any:
    offers = node.get("offers")
    candidates = [offers] if isinstance(offers, dict) else offers if isinstance(offers, list) else []
    for offer in candidates:
        if isinstance(offer, dict) and offer.get(key) not in (None, ""):
            return offer[key]
    return None


def _image_field(node: dict[str, Any]) -> str | None:
    image = node.get("image")
    if isinstance(image, list):
        return str(image[0]) if image else None
    return str(image) if image else None


def _cents(amount: Any) -> int | None:
    if amount is None:
        return None
    try:
        value = float(str(amount).replace(",", "."))
    except ValueError:
        return None
    return round(value * 100) or None


def _ean_in_fragment(fragment: str) -> str | None:
    """A 13-digit group that is an EAN, not a CDN hash.

    Thalia's cover URLs embed a 40-hex image hash whose leading digits can
    look like an EAN; the checksum is the only reliable way to tell them
    apart, and a wrong ISBN outranks a correct title in the matcher.
    """
    for token in re.findall(r"\d{13}", fragment):
        digits = [int(d) for d in token]
        check = (10 - (sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:-1])) % 10)) % 10
        if check == digits[-1]:
            return str(token)
    return None


def _isbn_from_path(external_id: str) -> str | None:
    """An EAN carried in the id, whether as an `ean=` query or a path segment."""
    match = re.search(r"[?&]ean=(\d{13})", external_id)
    if match:
        return match.group(1)
    return _ean_in_fragment(external_id)


class ShopProvider(BaseProvider):
    """Shared plumbing for the retail front-ends."""

    supports: ClassVar[frozenset[MediaClass]] = _ALL_CLASSES
    provides_price: ClassVar[bool] = True
    provides_rating: ClassVar[bool] = True
    requires_credentials: ClassVar[bool] = False
    #: Price and cover only; never bibliographic merge fields.
    shop: ClassVar[bool] = True

    def _headers(self) -> dict[str, str]:
        """Browser-shaped headers; a configured Cookie header (e.g. a Thalia session) unlocks the wall."""
        headers = {"User-Agent": CHROME_UA, **SHOP_HEADERS}
        if self.config.cookie:
            headers["Cookie"] = self.config.cookie
        return headers

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.get(
            url,
            provider=self.name,
            params=params,
            headers=self._headers(),
            rate_limit_per_minute=self.config.rate_limit_per_minute,
        )

    def _blocked_check(self, response: Any) -> None:
        if response.status == 403 or _CHALLENGE_MARKER in response.text:
            raise ShopBlocked(f"{self.name}: bot challenge (status {response.status})")

    def _fallback_query(self, query: MediaQuery) -> str | None:
        """Title (+ author) to search when an ISBN search came back empty."""
        if query.title:
            return f"{query.title} {query.author or ''}".strip()
        return None

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        return None


class ThaliaProvider(ShopProvider):
    name: ClassVar[str] = "thalia"
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.thalia.de"

    _TILE = re.compile(r'href="/shop/home/artikeldetails/(A\d+)')
    _AUTOR = re.compile(r'class="[^"]*tm-artikeldetails__autor[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
    _VERKAUF = re.compile(r'class="[^"]*tm-preis-wrapper__verkaufspreis[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
    _STREICH = re.compile(r'class="[^"]*tm-preis-wrapper__streichpreis[^"]*"[^>]*>(.*?)</(?:span|s)>', re.DOTALL)

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        term = query.isbn or f"{query.title} {query.author or ''}".strip()
        candidates = await self._tiles(term)
        if not candidates and query.title:
            fallback = self._fallback_query(query)
            if fallback and fallback != term:
                candidates = await self._tiles(fallback)
        return candidates

    async def _tiles(self, term: str) -> list[ProviderCandidate]:
        response = await self._get(f"{self.base_url}/suche", params={"sq": term})
        self._blocked_check(response)
        if response.status != 200:
            return []

        page = response.text
        matches = list(self._TILE.finditer(page))
        # A tile carries its id in href and in dl-product; the plan's href
        # regex matches once per tile, but be defensive about repeats.
        tiles: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(page)
            if tiles and tiles[-1][0] == match.group(1):
                tiles[-1] = (match.group(1), tiles[-1][1] + page[match.end() : end])
            else:
                tiles.append((match.group(1), page[match.end() : end]))

        candidates: list[ProviderCandidate] = []
        for product_id, tile in tiles:
            if "data-ad" in tile or "Gesponsert" in tile:
                continue
            # The anchor tag was opened before the href, so the segment
            # starts mid-attribute; the visible link text runs from the
            # closing `>` of that anchor to the first nested tag.
            head = tile[: tile.find("<")] if "<" in tile else tile
            title = _text(head[head.rfind(">") + 1 :] if ">" in head else head)
            if not title:
                title = _first(r'\bname="([^"]+)"', tile) or ""
            author = _text(_first(self._AUTOR.pattern, tile) or "")
            listed = _text(_first(self._STREICH.pattern, tile) or "")
            sale = _text(_first(self._VERKAUF.pattern, tile) or "")
            candidates.append(
                ProviderCandidate(
                    external_id=product_id,
                    title=title or product_id,
                    authors=[author] if author else [],
                    # The tile carries no EAN anywhere (verified on the recorded
                    # page); the cover URL's CDN hash looks like one and would
                    # outrank a correct title match, so the detail page is
                    # where the real gtin13 is read.
                    payload={"list_price_text": listed or None, "sale_price_text": sale or None},
                )
            )
        return candidates

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        response = await self._get(f"{self.base_url}/shop/home/artikeldetails/{external_id}")
        self._blocked_check(response)
        if response.status != 200:
            return None

        node = _product_node(response.text)
        if node is None:
            return None

        cents = _cents(_offer_field(node, "price"))
        cover = _image_field(node)
        if cents is None and cover is None:
            return None

        description = node.get("description")
        isbn = node.get("gtin13") or node.get("isbn") or None
        if isbn:
            digits = re.sub(r"\D", "", str(isbn))
            isbn = digits if len(digits) in (10, 13) else None

        rating = node.get("aggregateRating") or {}
        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            external_url=f"{self.base_url}/shop/home/artikeldetails/{external_id}",
            title=_text(str(node.get("name") or "")),
            isbn13=isbn,
            description=html_module.unescape(str(description)) if description else None,
            rating_value=float(rating["ratingValue"]) if rating.get("ratingValue") else None,
            rating_scale=5.0 if rating.get("ratingValue") else None,
            rating_count=int(rating["reviewCount"]) if rating.get("reviewCount") else None,
            list_price_cents=cents,
            list_price_currency=str(_offer_field(node, "priceCurrency") or "EUR"),
            cover_source_url=cover,
        )


class BuchkatalogProvider(ShopProvider):
    name: ClassVar[str] = "buchkatalog"
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.buchkatalog.de"

    #: The JSON search endpoint. POST is the endpoint's own shape; it 500s
    #: intermittently for anonymous callers, GET with `search=` answers the
    #: same query reliably and is what the recorded fixtures captured.
    _API = "/api/search/search"
    _API_PARAMS: ClassVar[dict[str, Any]] = {"storeId": 167206, "catalogId": 10002, "langId": -3}

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        term = query.isbn or f"{query.title} {query.author or ''}".strip()
        products = await self._search(term)
        if not products and query.title:
            fallback = self._fallback_query(query)
            if fallback and fallback != term:
                products = await self._search(fallback)
        return [self._candidate(product) for product in products]

    async def _search(self, term: str) -> list[dict[str, Any]]:
        response = await self._get(f"{self.base_url}{self._API}", params={**self._API_PARAMS, "search": term})
        self._blocked_check(response)
        if response.status != 200:
            return []
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return []
        products = (data.get("data") or {}).get("products") or []
        return [product for product in products if isinstance(product, dict)]

    def _candidate(self, product: dict[str, Any]) -> ProviderCandidate:
        price = product.get("price") or {}
        candidate = ProviderCandidate(
            external_id=str(product.get("id") or product.get("artNr") or ""),
            title=_text(str(product.get("name") or "")),
            authors=[str(product[key]) for key in ("author1", "author2", "author3") if product.get(key)],
            isbn13=product.get("isbn13") or None,
        )
        # The JSON answer carries price and cover outright, so the ladder
        # reads them without a second request; fetch() re-runs the same
        # lookup for a caller that has only an id.
        candidate.record = ProviderRecord(
            provider=self.name,
            external_id=candidate.external_id,
            external_url=f"{self.base_url}{product.get('seopath')}" if product.get("seopath") else None,
            title=candidate.title,
            authors=candidate.authors,
            isbn13=candidate.isbn13,
            publisher=product.get("publishingHouse") or None,
            description=product.get("description") or None,
            list_price_cents=_cents(price.get("value")),
            list_price_currency=str(price.get("currencyCode") or "EUR"),
            cover_source_url=_buchkatalog_cover(product, self.base_url),
        )
        return candidate

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        for product in await self._search(external_id):
            if str(product.get("id") or product.get("artNr")) == external_id:
                return self._candidate(product).record
        return None


class AmazonProvider(ShopProvider):
    name: ClassVar[str] = "amazon"
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.amazon.de"

    #: The JS challenge page Amazon serves to scripted clients: a meta
    #: refresh to a `bm-verify` URL, no results.
    _INTERSTITIAL = "bm-verify"
    _SPONSORED = re.compile(r'sp-sponsored-result|aria-label="Gesponsert|Gesponserte Anzeige')
    _ASIN_LINK = re.compile(r'href="(?:/gp/product|/dp)/([A-Z0-9]{10})')
    _PRICE_WHOLE = re.compile(r'class="a-price-whole"[^>]*>([\d.,]+?)<', re.DOTALL)
    _PRICE_FRACTION = re.compile(r'class="a-price-fraction"[^>]*>(\d{1,2})<', re.DOTALL)
    #: An organic hit opens as `div role="listitem"` carrying its ASIN; the
    #: sponsored carousels reuse `data-asin=` deeper in the markup without
    #: the role, so anchoring on the attribute alone would slice one huge
    #: card out of the footer and hand back a promo link as a result.
    _CARD_START = re.compile(r'<div[^>]*role="listitem"[^>]*data-asin="([A-Z0-9]{10})"')

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        term = query.isbn or f"{query.title} {query.author or ''}".strip()
        cards = await self._cards(term)
        if cards is None:
            raise ShopBlocked(f"{self.name}: bot interstitial")
        if not cards and query.title:
            fallback = self._fallback_query(query)
            if fallback and fallback != term:
                cards = await self._cards(fallback) or []
        return cards

    async def _cards(self, term: str) -> list[ProviderCandidate] | None:
        response = await self._get(f"{self.base_url}/s", params={"k": term})
        page = response.text
        if response.status == 403 or _CHALLENGE_MARKER in page or self._INTERSTITIAL in page:
            return None
        if "data-asin=" not in page:
            return None

        starts = [(match.start(), match.group(1)) for match in self._CARD_START.finditer(page)]
        candidates: list[ProviderCandidate] = []
        for index, (start, asin) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(page)
            card = page[start:end]
            if self._SPONSORED.search(card):
                continue
            heading = _first(r"<h2[^>]*>(.*?)</h2>", card)
            title = _text(heading or "")
            cents = self._card_price(card)
            candidate = ProviderCandidate(external_id=asin, title=title or asin)
            if cents is not None:
                candidate.record = ProviderRecord(
                    provider=self.name,
                    external_id=asin,
                    title=title or asin,
                    list_price_cents=cents,
                    list_price_currency="EUR",
                    external_url=f"{self.base_url}/dp/{asin}",
                )
            candidates.append(candidate)
        return candidates

    def _card_price(self, card: str) -> int | None:
        whole = _first(self._PRICE_WHOLE.pattern, card)
        if whole is None:
            return None
        fraction = _first(self._PRICE_FRACTION.pattern, card) or "0"
        return parse_price_cents(f"{whole}.{fraction}")

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        response = await self._get(f"{self.base_url}/dp/{external_id}")
        page = response.text
        if response.status == 403 or _CHALLENGE_MARKER in page or self._INTERSTITIAL in page:
            raise ShopBlocked(f"{self.name}: bot interstitial")
        if response.status != 200:
            return None

        node = _product_node(page)
        cents = _cents(_offer_field(node, "price")) if node else None
        cents = cents if cents is not None else self._dp_price(page)
        cover = (_image_field(node) if node else None) or _first(r'property="og:image" content="([^"]+)"', page)
        if cents is None and cover is None:
            return None

        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            external_url=f"{self.base_url}/dp/{external_id}",
            title=_text(str(node.get("name") or "")) if node else None,
            isbn13=_first(r'itemprop="isbn"[^>]*content="([^"]+)"', page) or _isbn_from_path(external_id),
            list_price_cents=cents,
            list_price_currency=str(_offer_field(node, "priceCurrency") or "EUR") if node else "EUR",
            cover_source_url=cover,
        )

    def _dp_price(self, page: str) -> int | None:
        meta = _first(r'property="og:price:amount" content="([^"]+)"', page)
        if meta is not None:
            return _cents(meta)
        match = re.search(r'<span class="a-price[^"]*">\s*<span class="a-offscreen">([^<]+)</span>', page)
        return parse_price_cents(match.group(1)) if match else None


class Buch7Provider(ShopProvider):
    name: ClassVar[str] = "buch7"
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.buch7.de"

    #: The search form's own field names, verified against the live form.
    _SEARCH_PARAMS: ClassVar[dict[str, Any]] = {"commit": "Suchen"}
    _LINK = re.compile(r'href="(/produkt/[^"]+)"(.*?)</a>', re.DOTALL)
    _MATOMO_PRICE = re.compile(r'data-matomo-price="([\d.]+)"')

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        params = {**self._SEARCH_PARAMS, "search": query.isbn or f"{query.title} {query.author or ''}".strip()}
        response = await self._get(f"{self.base_url}/suche", params=params)
        self._blocked_check(response)
        if response.status != 200:
            return []
        page = response.text

        # An ISBN search 302-redirects to the product page, and the follow
        # is invisible to us (the client caches status+body only), so the
        # page's own canonical og:url is the only witness that the answer
        # is a product rather than a list.
        product = _first(r'property="og:url" content="[^"]*?(/produkt/[^"]+)"', page)

        seen: set[str] = set()
        candidates: list[ProviderCandidate] = []
        for href, block in self._LINK.findall(page):
            if href in seen:
                continue
            seen.add(href)
            alt = _first(r'alt="Titelbild für &quot;([^&"]+)&quot;', block) or _first(
                r'alt="Titelbild für "([^"]+)"', block
            )
            author = _first(r'class="author"[^>]*>\s*<a[^>]*>(.*?)</a>', block)
            candidates.append(
                ProviderCandidate(
                    external_id=href,
                    title=_text(alt or "") or _text(block)[:80],
                    authors=[_text(author)] if author else [],
                    isbn13=_isbn_from_path(href),
                )
            )

        if not candidates and product:
            # Redirected straight to the product page: that page is the answer.
            record = await self.fetch(product)
            title = (record.title if record else None) or _first(r'property="og:title" content="([^"]+)"', page)
            candidates.append(
                ProviderCandidate(
                    external_id=product,
                    title=title or product,
                    isbn13=_isbn_from_path(product),
                    record=record,
                )
            )
        return candidates

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        response = await self._get(f"{self.base_url}{external_id}")
        self._blocked_check(response)
        if response.status != 200:
            return None
        page = response.text

        cents = _cents(_first(self._MATOMO_PRICE.pattern, page))
        if cents is None:
            button = (
                _first(
                    r'data-target="cart--edit-form.add"[^>]*>(.*?)</button>',
                    page,
                )
                or ""
            )
            cents = parse_price_cents(button)
        cover = _first(r'property="og:image" content="([^"]+)"', page)
        if cents is None and cover is None:
            return None

        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            external_url=f"{self.base_url}{external_id}",
            title=_first(r'property="og:title" content="([^"]+)"', page),
            isbn13=_isbn_from_path(external_id),
            # buch7 renders no server-side description at all (verified on the
            # recorded product page); inventing one from the alt text would be worse.
            description=None,
            list_price_cents=cents,
            list_price_currency="EUR",
            cover_source_url=cover,
        )


class LehmannsProvider(ShopProvider):
    name: ClassVar[str] = "lehmanns"
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.lehmanns.de"

    _ARTICLE = re.compile(r'<article class="row book-result".*?</article>', re.DOTALL)
    #: The main edition's price lives only in the add-to-cart gtag label;
    #: every `div.price` on the page belongs to another edition.
    _GTAG_PRICE = re.compile(r"Preis:([\d.]+)")

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        term = query.isbn or f"{query.title} {query.author or ''}".strip()
        candidates = await self._quick(term)
        # The quick search legitimately answers 0 hits for an ISBN whose
        # title the title query finds (verified on the live site).
        if not candidates and query.title:
            fallback = self._fallback_query(query)
            if fallback and fallback != term:
                candidates = await self._quick(fallback)
        return candidates

    async def _quick(self, term: str) -> list[ProviderCandidate]:
        response = await self._get(f"{self.base_url}/search/quick", params={"q": term})
        self._blocked_check(response)
        if response.status != 200:
            return []
        page = response.text

        seen: set[str] = set()
        candidates: list[ProviderCandidate] = []
        for block in self._ARTICLE.findall(page):
            path = _first(r'itemprop="url"[^>]*href="(/shop/[^"?]+)', block)
            if path is None or path in seen:
                continue
            seen.add(path)
            title = _text(_first(r'itemprop="name"[^>]*>(.*?)</span>', block) or "")
            authors = [_text(a) for a in re.findall(r'<meta itemprop="author" content="([^"]*)"', block)]
            cents = _cents(_first(r'itemprop="price" content="([\d.,]+)"', block))
            candidate = ProviderCandidate(
                external_id=path,
                title=title or path,
                authors=[a for a in authors if a],
                isbn13=_isbn_from_path(path),
            )
            # The tile's own offer price: a hit answers for the edition it
            # shows, so the ladder reads it without a second request.
            if cents is not None:
                candidate.record = ProviderRecord(
                    provider=self.name,
                    external_id=path,
                    external_url=f"{self.base_url}{path}",
                    title=title or path,
                    authors=candidate.authors,
                    isbn13=candidate.isbn13,
                    list_price_cents=cents,
                    list_price_currency="EUR",
                )
            candidates.append(candidate)

        # An ISBN search redirects to the product page itself; the page then
        # has no result tiles but does carry the requested work's link.
        if not candidates and term:
            product = _first(rf'href="(/shop/[^"]*{re.escape(term)}[^"?]*)', page)
            if product is not None:
                candidates.append(
                    ProviderCandidate(
                        external_id=product,
                        title=_text(_first(r"<h1[^>]*>(.*?)</h1>", page) or "") or product,
                        isbn13=_isbn_from_path(product),
                    )
                )
        return candidates

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        response = await self._get(f"{self.base_url}{external_id}")
        self._blocked_check(response)
        if response.status != 200:
            return None
        page = response.text

        cents = _cents(_first(self._GTAG_PRICE.pattern, page))
        cover = _first(r'name="og:image" content="(https://www\.lehmanns\.de/media/[^"]+)"', page)
        if cents is None and cover is None:
            return None

        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            external_url=f"{self.base_url}{external_id}",
            title=_first(r"<h1[^>]*>(.*?)</h1>", page),
            isbn13=_isbn_from_path(external_id),
            # No server-rendered description on the product page (verified).
            description=None,
            list_price_cents=cents,
            list_price_currency="EUR",
            cover_source_url=cover,
        )


class EbookDeProvider(ShopProvider):
    name: ClassVar[str] = "ebookde"
    DEFAULT_BASE_URL: ClassVar[str] = "https://www.ebook.de"
    #: One `article-item` per product shown — the results list and the
    #: sidebar recommendations share the shape.
    _TILE = re.compile(
        r'<div class="(?:search-item )?article-item">.*?(?=<div class="(?:search-item )?article-item">|$)',
        re.DOTALL,
    )
    #: Coverscans embed the work's EAN in the filename
    #: (`423/42335932_9783446274211_xl.jpg`), the only per-tile ISBN on a
    #: page that renders none in the open. Keyed by the product id so a
    #: neighbouring carousel's scan cannot be read as this tile's.
    _SCAN_TMPL = r"coverscans/\d+/{}_([0-9]{{13}})_"
    #: A tile can carry the book and a matching Kindle edition at once, so
    #: the price node classes decide which number is the tile's price: the
    #: rendered price first, then the old price behind a discount, then the
    #: list price — never a sibling edition's figure.
    _PRICE_NODES: ClassVar[tuple[str, ...]] = (
        r'<span class="price price-(?:default|current|reduced)">([\d.,]+)',
        r'<span class="price price-old">.*?<span class="price-value">([\d.,]+)',
        r'<span class="price">([\d.,]+)',
    )

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        term = query.isbn or f"{query.title} {query.author or ''}".strip()
        candidates = await self._results(term)
        if not candidates and query.title:
            fallback = self._fallback_query(query)
            if fallback and fallback != term:
                candidates = await self._results(fallback)
        return candidates

    async def _results(self, term: str) -> list[ProviderCandidate]:
        response = await self._get(f"{self.base_url}/de/search", params={"q": term})
        self._blocked_check(response)
        if response.status != 200:
            return []
        page = response.text

        seen: set[str] = set()
        candidates: list[ProviderCandidate] = []
        for block in self._TILE.findall(page):
            path = _first(r'href="(/de/product/\d+/[^"]*?\.html)"', block)
            if path is None or path in seen:
                continue
            seen.add(path)
            title = _text(_first(r'<div class="title">(.*?)</div>', block) or "")
            author = _first(r'class="author[^"]*"[^>]*>(.*?)</(?:div|a)>', block)
            isbn13 = _first(self._SCAN_TMPL.format(path.split("/")[3]), block)
            cents: int | None = None
            for node in self._PRICE_NODES:
                raw = _first(node, block)
                if raw is not None:
                    cents = _cents(raw)
                    break
            candidate = ProviderCandidate(
                external_id=path,
                title=title or path,
                authors=[_text(author)] if author else [],
                isbn13=isbn13,
            )
            if cents is not None:
                candidate.record = ProviderRecord(
                    provider=self.name,
                    external_id=path,
                    external_url=f"{self.base_url}{path}",
                    title=title or path,
                    authors=candidate.authors,
                    isbn13=isbn13,
                    list_price_cents=cents,
                    list_price_currency="EUR",
                )
            candidates.append(candidate)
        return candidates

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        response = await self._get(f"{self.base_url}{external_id}")
        self._blocked_check(response)
        if response.status != 200:
            return None

        node = _product_node(response.text)
        if node is None:
            return None

        cents = _cents(_offer_field(node, "price"))
        cover = _image_field(node)
        if cents is None and cover is None:
            return None

        gtin = node.get("gtin13") or node.get("isbn")
        return ProviderRecord(
            provider=self.name,
            external_id=external_id,
            external_url=f"{self.base_url}{external_id}",
            title=_text(str(node.get("name") or "")),
            isbn13=str(gtin) if gtin else _isbn_from_path(external_id),
            publisher=str(node.get("brand", {}).get("name") or "") or None,
            # Only a publisher imprint string exists (verified), not a description.
            description=None,
            list_price_cents=cents,
            list_price_currency=str(_offer_field(node, "priceCurrency") or "EUR"),
            cover_source_url=cover,
        )


def _buchkatalog_cover(product: dict[str, Any], base_url: str) -> str | None:
    images = product.get("images") or []
    first = images[0] if images and isinstance(images[0], dict) else None
    url = first.get("url") if first else None
    if not url:
        return None
    return str(url) if str(url).startswith("http") else f"{base_url}{url}"
