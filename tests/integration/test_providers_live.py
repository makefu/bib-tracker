"""Live provider checks against the real services.

Marked ``live``; excluded from the Nix build, which has no network.
"""

from __future__ import annotations

import httpx
import pytest

from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata import build_provider
from bib_tracker.metadata.base import MediaQuery, ProviderConfig, ProviderStatus
from bib_tracker.metadata.http import CachedClient

pytestmark = pytest.mark.live


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        yield CachedClient(db, client)


async def test_boardgamegeek_answers_with_a_token(http, bgg_token) -> None:
    """BGG refuses anonymous requests outright, so the token is the whole
    difference between having board-game ratings and not."""
    provider = build_provider(ProviderConfig(name="bgg", api_key=bgg_token), http)
    assert provider.available() is True

    record = await provider.fetch("13")

    assert record is not None
    assert record.status is ProviderStatus.OK
    assert record.title == "Catan"
    assert record.rating_value is not None
    assert record.rating_scale == 10.0
    assert record.rating_count and record.rating_count > 1000


async def test_boardgamegeek_search_then_fetch(http, bgg_token) -> None:
    provider = build_provider(ProviderConfig(name="bgg", api_key=bgg_token), http)

    candidates = await provider.search(MediaQuery(media_class=MediaClass.GAME, title="Azul"))

    assert candidates
    record = await provider.fetch(candidates[0].external_id)
    assert record is not None
    assert record.status is ProviderStatus.OK


async def test_open_library_is_reachable_without_a_key(http) -> None:
    provider = build_provider(ProviderConfig(name="openlibrary"), http)

    record = await provider.fetch("isbn:9780451524935")

    assert record is not None
    assert record.status is ProviderStatus.OK
    assert record.title == "Nineteen Eighty-Four"


async def test_wikidata_covers_a_game_without_a_key(http) -> None:
    """The anonymous stand-in for BoardGameGeek."""
    provider = build_provider(ProviderConfig(name="wikidata"), http)

    candidates = await provider.search(MediaQuery(media_class=MediaClass.GAME, title="Carcassonne"))

    assert candidates, "Wikidata returned no board game for a very well known one"
