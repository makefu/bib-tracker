"""Live price lookups against the real services.

Marked ``live`` and excluded from the Nix build, which has no network. Run with
``nix run .#integration-prices`` or ``pytest -m live``.

These exist because the price ladder is exactly the kind of thing unit tests
cannot vouch for: the question is whether the DNB really carries a German
retail price for the books this household actually borrows, and only the DNB
can answer that.
"""

from __future__ import annotations

import httpx
import pytest

from bib_tracker.config import Settings
from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata import build_provider
from bib_tracker.metadata.base import MediaQuery, ProviderConfig, ProviderStatus
from bib_tracker.metadata.http import CachedClient

pytestmark = pytest.mark.live

#: Real titles from the tracked accounts, with the price the DNB records.
KNOWN_PRICES = [
    ("9783473460625", 1999),  # Hase Hibiskus
    ("9783737336284", 1300),  # Duden 36+
]


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        yield CachedClient(db, client)


@pytest.mark.parametrize(("isbn", "expected_cents"), KNOWN_PRICES)
async def test_the_dnb_really_has_a_price_for_these_books(http, isbn, expected_cents) -> None:
    provider = build_provider(ProviderConfig(name="dnb"), http)

    candidates = await provider.search(MediaQuery(media_class=MediaClass.BOOK, title="", isbn=isbn))

    assert candidates, f"the DNB knows nothing about {isbn}"
    record = await provider.fetch(candidates[0].external_id)
    assert record is not None
    assert record.status is ProviderStatus.OK
    assert record.list_price_cents == expected_cents
    assert record.list_price_currency == "EUR"


async def test_the_dnb_price_survives_the_whole_ladder(db, http) -> None:
    """End to end: look the price up, store it, and let the ladder choose it."""
    from bib_tracker.metadata.pricing import PriceBasis, preferred_provider_price, store_price

    isbn, expected = KNOWN_PRICES[0]
    settings = Settings(db_path=db.path, price_providers=["vlb", "dnb", "googlebooks"])

    async with db.write() as w:
        media_id = await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, isbn13, first_seen_at, last_seen_at)
            VALUES ('live', 'book', 'Hase Hibiskus', ?, datetime('now'), datetime('now'))
            """,
            (isbn,),
        )

    provider = build_provider(ProviderConfig(name="dnb"), http)
    candidates = await provider.search(MediaQuery(media_class=MediaClass.BOOK, title="", isbn=isbn))
    record = await provider.fetch(candidates[0].external_id)
    assert record is not None

    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO metadata_records (media_id, provider, status, list_price_cents,
                                          list_price_currency, fetched_at)
            VALUES (?, 'dnb', 'ok', ?, 'EUR', datetime('now'))
            """,
            (media_id, record.list_price_cents),
        )

    price = await preferred_provider_price(db, settings, media_id)
    assert price is not None
    assert price.cents == expected
    assert price.basis is PriceBasis.PROVIDER
    assert price.is_estimate is False

    await store_price(db, media_id, price, source="dnb")
    row = await db.fetch_one("SELECT effective_price_cents, price_basis FROM media WHERE id = ?", (media_id,))
    assert row["effective_price_cents"] == expected
    assert row["price_basis"] == "provider_list_price"


async def test_vlb_says_it_needs_credentials_rather_than_failing(http) -> None:
    """Without a VLB contract the ladder must fall through, not break."""
    provider = build_provider(ProviderConfig(name="vlb"), http)

    assert provider.available() is False
    record = await provider.fetch(KNOWN_PRICES[0][0])
    assert record is not None
    assert record.status is ProviderStatus.NEEDS_CREDENTIALS
