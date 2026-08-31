"""Providers, against payloads recorded from the real services."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata import build_provider
from bib_tracker.metadata.base import MediaQuery, ProviderConfig, ProviderStatus
from bib_tracker.metadata.http import CachedClient

PROVIDERS = Path(__file__).parent / "fixtures" / "providers"

#: MARC brackets non-filing characters with these; they must never survive.
NON_FILING = ("\u0098", "\u009c")


def payload(name: str) -> str:
    return (PROVIDERS / name).read_text(encoding="utf-8")


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient() as client:
        yield CachedClient(db, client)


def _provider(name: str, http, base_url: str = "http://meta.test", api_key: str | None = None):
    return build_provider(ProviderConfig(name=name, base_url=base_url, api_key=api_key), http)


BOOK = MediaQuery(media_class=MediaClass.BOOK, title="Die unendliche Geschichte", author="Ende, Michael")


# -- Open Library -----------------------------------------------------------


@respx.mock
async def test_open_library_search_ranks_real_results(http) -> None:
    respx.get("http://meta.test/search.json").mock(
        return_value=httpx.Response(200, text=payload("openlibrary_search.json"))
    )
    provider = _provider("openlibrary", http)

    candidates = await provider.search(BOOK)

    assert candidates
    assert candidates[0].title == "Die unendliche Geschichte"
    assert "Michael Ende" in candidates[0].authors
    assert candidates[0].year == 1979


@respx.mock
async def test_open_library_work_carries_its_rating_on_its_own_scale(http) -> None:
    respx.get("http://meta.test/search.json").mock(
        return_value=httpx.Response(200, text=payload("openlibrary_search.json"))
    )
    provider = _provider("openlibrary", http)

    record = await provider.fetch("/works/OL2271589W")

    assert record is not None
    assert record.status is ProviderStatus.OK
    assert record.rating_value == pytest.approx(4.116667)
    assert record.rating_scale == 5.0
    assert record.rating_count == 60
    assert record.cover_source_url and record.cover_source_url.endswith("-L.jpg")


@respx.mock
async def test_open_library_isbn_lookup(http) -> None:
    respx.get("http://meta.test/api/books").mock(
        return_value=httpx.Response(200, text=payload("openlibrary_isbn.json"))
    )
    provider = _provider("openlibrary", http)

    record = await provider.fetch("isbn:9780451524935")

    assert record is not None
    assert record.title == "Nineteen Eighty-Four"
    assert record.isbn13 == "9780451524935"
    assert record.page_count == 328
    assert record.publisher == "Signet Classics"
    assert record.published_year == 1993


@respx.mock
async def test_open_library_reports_an_unknown_isbn_as_not_found(http) -> None:
    respx.get("http://meta.test/api/books").mock(return_value=httpx.Response(200, text="{}"))
    provider = _provider("openlibrary", http)

    record = await provider.fetch("isbn:9999999999999")

    assert record is not None
    assert record.status is ProviderStatus.NOT_FOUND


# -- DNB --------------------------------------------------------------------


@respx.mock
async def test_dnb_parses_marc_records(http) -> None:
    respx.get("http://meta.test").mock(return_value=httpx.Response(200, text=payload("dnb_sru.xml")))
    provider = _provider("dnb", http)

    candidates = await provider.search(BOOK)

    assert candidates
    assert candidates[0].title == "Die unendliche Geschichte"
    assert candidates[0].isbn13 == "9783126741316"
    assert candidates[0].year == 2025


@respx.mock
async def test_dnb_strips_marc_non_filing_markers(http) -> None:
    """MARC brackets the article with control codes. They are not part of the
    title and would otherwise leak into fingerprints and into the interface."""
    respx.get("http://meta.test").mock(return_value=httpx.Response(200, text=payload("dnb_sru.xml")))
    provider = _provider("dnb", http)

    candidates = await provider.search(BOOK)

    assert candidates[0].title.startswith("Die ")
    for marker in NON_FILING:
        assert marker not in candidates[0].title


@respx.mock
async def test_dnb_does_not_repeat_an_author_listed_twice(http) -> None:
    """MARC often records the same person under both 100 and 700."""
    respx.get("http://meta.test").mock(return_value=httpx.Response(200, text=payload("dnb_sru.xml")))
    provider = _provider("dnb", http)

    candidates = await provider.search(BOOK)

    assert candidates[1].authors == ["Ende, Michael"]


# -- Google Books -----------------------------------------------------------


@respx.mock
async def test_google_books_quota_is_an_error_not_an_absence(http) -> None:
    """Recording "not found" here would permanently mislabel a work that
    Google simply refused to talk about today."""
    respx.get(url__regex=r"http://meta\.test/volumes/.*").mock(
        return_value=httpx.Response(429, text=payload("googlebooks_quota_exceeded.json"))
    )
    provider = _provider("googlebooks", http)

    record = await provider.fetch("abc123")

    assert record is not None
    assert record.status is ProviderStatus.ERROR


@respx.mock
async def test_google_books_extracts_the_list_price(http) -> None:
    volume = {
        "id": "vol1",
        "volumeInfo": {
            "title": "Die unendliche Geschichte",
            "authors": ["Michael Ende"],
            "publishedDate": "2004-01-01",
            "pageCount": 480,
            "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9783522621885"}],
            "imageLinks": {"thumbnail": "http://books.google.com/x.jpg"},
            "averageRating": 4.5,
            "ratingsCount": 12,
        },
        "saleInfo": {"listPrice": {"amount": 24.0, "currencyCode": "EUR"}},
    }
    respx.get(url__regex=r"http://meta\.test/volumes/.*").mock(
        return_value=httpx.Response(200, text=json.dumps(volume))
    )
    provider = _provider("googlebooks", http)

    record = await provider.fetch("vol1")

    assert record is not None
    assert record.list_price_cents == 2400
    assert record.list_price_currency == "EUR"
    assert record.page_count == 480
    # Google still serves plain-http thumbnails, which an https page blocks.
    assert record.cover_source_url == "https://books.google.com/x.jpg"


# -- BoardGameGeek ----------------------------------------------------------


async def test_bgg_without_credentials_is_unavailable_not_broken(http) -> None:
    """BGG answers 401 to anonymous requests, so with no key it must report
    that it needs one rather than look like a failure."""
    provider = _provider("bgg", http)

    assert provider.requires_credentials is True
    assert provider.available() is False

    record = await provider.fetch("13")
    assert record is not None
    assert record.status is ProviderStatus.NEEDS_CREDENTIALS


@respx.mock
async def test_bgg_reports_a_rejected_credential_as_needing_one(http) -> None:
    respx.get("http://meta.test/thing").mock(
        return_value=httpx.Response(401, text=payload("boardgamegeek_unauthorized.xml"))
    )
    provider = _provider("bgg", http, api_key="stale")

    record = await provider.fetch("13")

    assert record is not None
    assert record.status is ProviderStatus.NEEDS_CREDENTIALS


@respx.mock
async def test_bgg_keeps_its_rating_on_a_ten_point_scale(http) -> None:
    xml = """<?xml version="1.0"?><items>
      <item type="boardgame" id="13">
        <name type="primary" value="Catan"/>
        <yearpublished value="1995"/>
        <image>https://cf.geekdo-images.com/catan.jpg</image>
        <description>Trade, build, settle.</description>
        <link type="boardgamepublisher" value="Kosmos"/>
        <statistics><ratings>
          <usersrated value="94000"/><average value="7.8"/>
        </ratings></statistics>
      </item></items>"""
    respx.get("http://meta.test/thing").mock(return_value=httpx.Response(200, text=xml))
    provider = _provider("bgg", http, api_key="token")

    record = await provider.fetch("13")

    assert record is not None
    assert record.title == "Catan"
    assert record.rating_value == 7.8
    # Not rescaled to five: the interface should say "7,8/10".
    assert record.rating_scale == 10.0
    assert record.rating_count == 94000
    assert record.publisher == "Kosmos"


# -- Wikidata ---------------------------------------------------------------


@respx.mock
async def test_wikidata_covers_games_without_any_credential(http) -> None:
    """Which is why it is the default for games now that BGG is gated."""
    results = {
        "results": {
            "bindings": [
                {
                    "itemLabel": {"value": "Catan"},
                    "year": {"value": "1995"},
                    "publisherLabel": {"value": "Kosmos"},
                    "image": {"value": "https://commons.wikimedia.org/catan.jpg"},
                    "description": {"value": "Brettspiel von Klaus Teuber"},
                }
            ]
        }
    }
    respx.get("http://meta.test/sparql").mock(return_value=httpx.Response(200, text=json.dumps(results)))
    provider = _provider("wikidata", http)

    assert provider.available() is True
    record = await provider.fetch("Q170392")

    assert record is not None
    assert record.title == "Catan"
    assert record.published_year == 1995
    assert record.publisher == "Kosmos"
    # Wikidata has no community rating, and must not invent one.
    assert record.rating_value is None


def test_every_registered_provider_declares_what_it_supports() -> None:
    from bib_tracker.metadata import PROVIDER_FACTORIES

    for name, factory in PROVIDER_FACTORIES.items():
        assert factory.name == name
        assert factory.supports, f"{name} supports no media class"


@respx.mock
async def test_dnb_fetches_by_its_own_record_number(http) -> None:
    """search() returns the DNB's IDN, so fetch() has to look up that index --
    NID, which reads plausibly, matches nothing at all."""
    route = respx.get("http://meta.test").mock(return_value=httpx.Response(200, text=payload("dnb_sru.xml")))
    provider = _provider("dnb", http)

    await provider.fetch("1374532193")

    assert route.called
    query = dict(route.calls[0].request.url.params)
    assert query["query"] == "IDN=1374532193"
