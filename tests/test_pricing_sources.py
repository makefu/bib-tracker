"""Where a purchase price comes from, and in what order."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bib_tracker.metadata import build_provider
from bib_tracker.metadata.base import ProviderConfig, ProviderStatus
from bib_tracker.metadata.dnb import parse_sru
from bib_tracker.metadata.http import CachedClient
from bib_tracker.metadata.pricing import PriceBasis, all_found_prices, resolve_price
from bib_tracker.metadata.vlb import extract_price

PROVIDERS = Path(__file__).parent / "fixtures" / "providers"


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient() as client:
        yield CachedClient(db, client)


@pytest.fixture
async def media_id(db):
    async with db.write() as w:
        return await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, first_seen_at, last_seen_at)
            VALUES ('k', 'book', 'Hase Hibiskus', datetime('now'), datetime('now'))
            """
        )


async def _record(db, media_id: int, provider: str, cents: int | None) -> None:
    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO metadata_records (media_id, provider, status, list_price_cents,
                                          list_price_currency, fetched_at)
            VALUES (?, ?, 'ok', ?, 'EUR', datetime('now'))
            """,
            (media_id, provider, cents),
        )


# -- DNB carries the price in MARC 020 $c ------------------------------------


def test_dnb_extracts_the_german_retail_price() -> None:
    """Recorded from the live SRU endpoint: 'Festeinband : EUR 19.99 (DE), ...'."""
    records = parse_sru((PROVIDERS / "dnb_price_bound.xml").read_text(encoding="utf-8"))

    assert records[0].list_price_cents == 1999
    assert records[0].list_price_currency == "EUR"
    assert records[0].payload["price_note"] is None


def test_dnb_marks_an_approximate_price_as_such() -> None:
    """'circa EUR 16.00 (DE)' is a recommendation, not a bound price."""
    records = parse_sru((PROVIDERS / "dnb_price_circa.xml").read_text(encoding="utf-8"))

    assert records[0].list_price_cents == 1600
    assert records[0].payload["price_note"] == "circa"


def test_dnb_prefers_the_german_price_over_the_austrian_one() -> None:
    records = parse_sru((PROVIDERS / "dnb_price_bound.xml").read_text(encoding="utf-8"))
    # The record also lists EUR 20.60 (AT) and CHF 28.50.
    assert records[0].list_price_cents == 1999


def test_a_record_without_a_price_reports_none() -> None:
    """The second record in this response carries no 020 $c at all."""
    records = parse_sru((PROVIDERS / "dnb_sru.xml").read_text(encoding="utf-8"))

    assert records[0].list_price_cents == 1100  # "Broschur : EUR 11.00 (DE)"
    assert records[1].list_price_cents is None


# -- VLB ---------------------------------------------------------------------


def test_vlb_prefers_a_bound_price_over_a_recommendation() -> None:
    cents, bound = extract_price(
        {
            "prices": [
                {"priceType": "04", "priceAmount": 21.0, "currencyCode": "EUR", "countryCode": "DE"},
                {"priceType": "02", "priceAmount": 19.99, "currencyCode": "EUR", "countryCode": "DE"},
            ]
        }
    )
    assert cents == 1999
    assert bound is True


def test_vlb_ignores_prices_for_other_countries_and_currencies() -> None:
    cents, _ = extract_price(
        {
            "prices": [
                {"priceType": "02", "priceAmount": 28.5, "currencyCode": "CHF", "countryCode": "CH"},
                {"priceType": "02", "priceAmount": 20.6, "currencyCode": "EUR", "countryCode": "AT"},
                {"priceType": "02", "priceAmount": 19.99, "currencyCode": "EUR", "countryCode": "DE"},
            ]
        }
    )
    assert cents == 1999


def test_vlb_without_a_token_says_it_needs_one(http) -> None:
    provider = build_provider(ProviderConfig(name="vlb"), http)
    assert provider.requires_credentials is True
    assert provider.available() is False


@respx.mock
async def test_vlb_reports_a_rejected_token_as_needing_credentials(http) -> None:
    respx.get(url__regex=r"http://vlb\.test/product/.*").mock(return_value=httpx.Response(401))
    provider = build_provider(ProviderConfig(name="vlb", base_url="http://vlb.test", api_key="stale"), http)

    record = await provider.fetch("9783473460625")

    assert record is not None
    assert record.status is ProviderStatus.NEEDS_CREDENTIALS


@respx.mock
async def test_vlb_returns_the_bound_price(http) -> None:
    respx.get(url__regex=r"http://vlb\.test/product/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "productId": "abc",
                "title": "Hase Hibiskus",
                "isbn13": "9783473460625",
                "publisher": "Ravensburger",
                "publicationDate": "2023-01-15",
                "prices": [{"priceType": "02", "priceAmount": 19.99, "currencyCode": "EUR", "countryCode": "DE"}],
            },
        )
    )
    provider = build_provider(ProviderConfig(name="vlb", base_url="http://vlb.test", api_key="token"), http)

    record = await provider.fetch("9783473460625")

    assert record is not None
    assert record.list_price_cents == 1999
    assert record.payload["price_is_bound"] is True
    assert record.external_url.endswith("9783473460625")


# -- the ladder --------------------------------------------------------------


async def test_the_configured_order_decides_which_price_wins(db, settings, media_id) -> None:
    await _record(db, media_id, "googlebooks", 2400)
    await _record(db, media_id, "dnb", 1999)
    await _record(db, media_id, "vlb", 1899)

    settings.price_providers = ["vlb", "dnb", "googlebooks"]
    assert [name for name, _ in await all_found_prices(db, settings, media_id)] == [
        "vlb",
        "dnb",
        "googlebooks",
    ]
    assert (await resolve_price(db, settings, media_id, "book")).cents == 1899

    settings.price_providers = ["dnb", "googlebooks"]
    assert (await resolve_price(db, settings, media_id, "book")).cents == 1999

    settings.price_providers = ["googlebooks"]
    assert (await resolve_price(db, settings, media_id, "book")).cents == 2400


async def test_the_ladder_falls_through_to_a_lower_preference(db, settings, media_id) -> None:
    """The VLB needs a contract, so most installations will land on the DNB."""
    await _record(db, media_id, "dnb", 1999)
    settings.price_providers = ["vlb", "dnb", "googlebooks"]

    price = await resolve_price(db, settings, media_id, "book")

    assert price.cents == 1999
    assert price.basis is PriceBasis.PROVIDER
    assert price.is_estimate is False


async def test_no_provider_price_falls_back_to_the_class_default(db, settings, media_id) -> None:
    price = await resolve_price(db, settings, media_id, "book")

    assert price.cents == 1500
    assert price.basis is PriceBasis.DEFAULT
    assert price.is_estimate is True


async def test_a_price_you_typed_still_beats_every_provider(db, settings, media_id) -> None:
    from bib_tracker.metadata.pricing import Price, store_price

    await _record(db, media_id, "vlb", 1899)
    await store_price(db, media_id, Price(999, PriceBasis.MANUAL), source="manual")

    price = await resolve_price(db, settings, media_id, "book")

    assert price.cents == 999
    assert price.basis is PriceBasis.MANUAL


async def test_a_provider_outside_the_preference_list_still_beats_a_guess(db, settings, media_id) -> None:
    await _record(db, media_id, "openlibrary", 1234)
    settings.price_providers = ["vlb", "dnb"]

    price = await resolve_price(db, settings, media_id, "book")

    assert price.cents == 1234
    assert price.basis is PriceBasis.PROVIDER
