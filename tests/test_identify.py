"""The identify dialog's backend: candidate cards, the picker's apply.

The providers here are stubs that answer with one recorded-shaped record; the
shop parsers themselves are pinned in ``test_shop_providers.py``. What this
file owns is the behaviour the dialog depends on and no provider test covers:
nothing is filtered by the matcher's threshold, an apply re-fetches rather
than trusting the tile, and a confirmed shop record may fill a bibliographic
gap without ever outranking a catalogue.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx
import pytest

from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata import PROVIDER_FACTORIES, identify
from bib_tracker.metadata.base import (
    BaseProvider,
    MatchMethod,
    MediaQuery,
    ProviderCandidate,
    ProviderConfig,
    ProviderRecord,
    ProviderStatus,
)
from bib_tracker.metadata.http import CachedClient
from bib_tracker.metadata.merge import FIELD_PRECEDENCE, merge_records
from bib_tracker.metadata.shops import ShopBlocked
from bib_tracker.metadata.worker import apply_records, store_record


def _record(provider: str, external_id: str) -> ProviderRecord:
    """The one answer every stub gives, as a provider record."""
    return ProviderRecord(
        provider=provider,
        external_id=external_id,
        status=ProviderStatus.OK,
        external_url=f"https://stub.example/{external_id}",
        title="Unterwerfung",
        authors=["Michel Houellebecq"],
        isbn13="9783462056344",
        published_year=2015,
        publisher="Kiepenheuer & Witsch",
        description="Paris, kurz vor der Präsidentschaftswahl.",
        list_price_cents=2299,
        list_price_currency="EUR",
        cover_source_url="https://stub.example/cover.jpg",
    )


class StubProvider(BaseProvider):
    name = "dnb"
    supports = frozenset(MediaClass)
    provides_price = True
    shop = False

    #: The apply path builds its own provider instance, so the fetch log has
    #: to be readable from the test's instance too.
    fetched: ClassVar[list[str]] = []

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        return [
            ProviderCandidate(
                external_id="a1",
                title=query.title,
                authors=["Michel Houellebecq"],
                isbn13=query.isbn,
            )
        ]

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        type(self).fetched.append(external_id)
        return _record(self.name, external_id)


class BlockedProvider(StubProvider):
    name = "blocked"

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        raise ShopBlocked("blocked: bot challenge")


class ShopStub(StubProvider):
    """A shop-shaped stub: the record travels on the candidate, so a card
    costs no second request."""

    name = "shopstub"
    shop = True

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        return [
            ProviderCandidate(external_id="s1", title=query.title, isbn13=query.isbn, record=_record(self.name, "s1"))
        ]


class NoResultProvider(StubProvider):
    name = "empty"

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        return []

    async def fetch(self, external_id: str) -> ProviderRecord | None:
        return None


class ManyProvider(StubProvider):
    """Nine unrelated editions, none of which a machine would accept."""

    name = "many"

    async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
        return [ProviderCandidate(external_id=f"x{i}", title=f"Fremde Ausgabe {i}") for i in range(9)]


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    for cls in (StubProvider, BlockedProvider, ShopStub, NoResultProvider, ManyProvider):
        monkeypatch.setitem(PROVIDER_FACTORIES, cls.name, cls)
    StubProvider.fetched = []
    # The ladders run at the end of an apply; left at their defaults they
    # would reach the network from a unit test.
    settings.price_providers = []
    settings.image_providers = []


@pytest.fixture
def query() -> MediaQuery:
    return MediaQuery(media_class=MediaClass.BOOK, title="Unterwerfung", author="Michel Houellebecq")


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient() as client:
        yield CachedClient(db, client)


@pytest.fixture
async def media_id(db) -> int:
    async with db.write() as w:
        return int(
            await w.execute(
                """
                INSERT INTO media (media_key, media_class, title, first_seen_at, last_seen_at)
                VALUES ('stub', 'book', 'ohne titel', datetime('now'), datetime('now'))
                """
            )
        )


def _stub(http: Any, name: str = "dnb") -> BaseProvider:
    return PROVIDER_FACTORIES[name](ProviderConfig(name=name, rate_limit_per_minute=6000), http)


async def _seed_other(db: Any, key: str) -> int:
    """A second work that already carries the ISBN the stub answers with."""
    async with db.write() as w:
        return int(
            await w.execute(
                "INSERT INTO media (media_key, media_class, title, isbn13, first_seen_at, last_seen_at)"
                " VALUES (?, 'book', 'Unterwerfung', '9783462056344', datetime('now'), datetime('now'))",
                (key,),
            )
        )


# -- query parsing -----------------------------------------------------------


def test_a_loose_isbn_becomes_an_isbn_query() -> None:
    q = identify.parse_query("book", "978-3-832-16359-4")
    assert q.isbn == "9783832163594"
    # The text stays the title: a shop whose ISBN search finds nothing falls
    # back to the title, and that fallback has to say something.
    assert q.title == "978-3-832-16359-4"


def test_an_isbn10_is_widened_to_isbn13() -> None:
    assert identify.as_isbn13("3-832-16359-X") == "9783832163594"


def test_free_text_never_becomes_an_isbn() -> None:
    # A wrong stored ISBN distorts every later match score, so free text only
    # carries the ISBN the row already has.
    q = identify.parse_query("book", "Unterwerfung Houellebecq", "9783462056344")
    assert q.isbn == "9783462056344"
    assert q.title == "Unterwerfung Houellebecq"
    assert identify.parse_query("book", "Unterwerfung Houellebecq").isbn is None


def test_an_unknown_media_class_is_other() -> None:
    assert identify.parse_query("volkslieder", "x").media_class is MediaClass.OTHER


# -- provider selection ------------------------------------------------------


def test_the_union_of_the_three_lists_is_asked(settings: Any) -> None:
    settings.metadata_providers = ["openlibrary", "dnb"]
    settings.price_providers = ["vlb", "thalia"]
    settings.image_providers = ["library", "thalia"]
    assert identify.provider_names(settings) == ["openlibrary", "dnb", "vlb", "thalia"]


def test_only_sources_that_know_the_media_class_are_built(settings: Any, http: Any) -> None:
    settings.metadata_providers = ["dnb", "bgg"]
    names = identify.usable_names(settings, "book")
    assert "dnb" in names
    # A board-game database asked about a novel returns noise it scores as a
    # match; the media class gates it here exactly as in the worker.
    assert "bgg" not in names


# -- cards -------------------------------------------------------------------


async def test_every_tile_is_shown_not_only_the_accepted_ones(http: Any, query: MediaQuery) -> None:
    """The matcher's thresholds decide what a machine may adopt. Here a person
    decides, so ranking only orders the list and nothing is dropped."""
    cards = await identify.search_cards(_stub(http, "many"), query)
    assert len(cards) == identify.MAX_CARDS_PER_PROVIDER
    assert all(card["score"] < 0.90 for card in cards)


async def test_a_card_carries_the_fetched_record(http: Any, query: MediaQuery) -> None:
    cards = await identify.search_cards(_stub(http), query)
    assert len(cards) == 1
    card = cards[0]
    assert card["provider"] == "dnb"
    assert card["external_id"] == "a1"
    assert card["title"] == "Unterwerfung"
    assert card["authors"] == "Michel Houellebecq"
    assert card["publisher"] == "Kiepenheuer & Witsch"
    assert card["year"] == 2015
    assert card["price_cents"] == 2299
    assert card["cover_url"] == "https://stub.example/cover.jpg"
    assert StubProvider.fetched == ["a1"]


async def test_a_tile_that_carries_its_record_costs_no_second_request(http: Any, query: MediaQuery) -> None:
    cards = await identify.search_cards(_stub(http, "shopstub"), query)
    assert cards[0]["price_cents"] == 2299
    assert StubProvider.fetched == []


async def test_a_provider_that_answers_nothing_yields_no_cards(http: Any, query: MediaQuery) -> None:
    assert await identify.search_cards(_stub(http, "empty"), query) == []


# -- apply -------------------------------------------------------------------


async def test_applying_stores_a_confirmed_manual_record_and_merges(
    db: Any, settings: Any, http: Any, media_id: int
) -> None:
    final = await identify.apply_choice(db, settings, http, media_id, "dnb", "a1")
    assert final == media_id

    row = await db.fetch_one("SELECT * FROM metadata_records WHERE media_id = ? AND provider = 'dnb'", (media_id,))
    assert row is not None
    assert row["confirmed"] == 1
    assert row["match_method"] == MatchMethod.MANUAL
    assert row["match_confidence"] == 1.0

    media = await db.fetch_one("SELECT title, author, isbn13, metadata_state FROM media WHERE id = ?", (final,))
    assert media is not None
    assert media["title"] == "Unterwerfung"
    assert media["author"] == "Michel Houellebecq"
    assert media["isbn13"] == "9783462056344"
    assert media["metadata_state"] == "enriched"


async def test_the_tile_is_not_trusted_the_record_is_refetched(
    db: Any, settings: Any, http: Any, media_id: int
) -> None:
    """The card comes from a search tile; the write has to come from the
    record behind it, or a stale tile would write stale data forever."""
    await identify.apply_choice(db, settings, http, media_id, "dnb", "a1")
    assert StubProvider.fetched == ["a1"]


async def test_applying_a_vanished_result_is_a_lookup_error(db: Any, settings: Any, http: Any, media_id: int) -> None:
    with pytest.raises(LookupError):
        await identify.apply_choice(db, settings, http, media_id, "empty", "a1")


async def test_applying_an_unconfigured_source_is_a_runtime_error(
    db: Any, settings: Any, http: Any, media_id: int
) -> None:
    with pytest.raises(RuntimeError):
        await identify.apply_choice(db, settings, http, media_id, "nosuchprovider", "a1")


async def test_a_picked_isbn_merges_the_row_into_the_work_that_has_it(
    db: Any, settings: Any, http: Any, media_id: int
) -> None:
    """The picked edition resolves to an ISBN another row already carries. The
    apply response has to name the surviving id or the browser lands on a row
    that no longer exists."""
    other = await _seed_other(db, "other")
    final = await identify.apply_choice(db, settings, http, media_id, "dnb", "a1")
    assert final == other
    assert await db.fetch_one("SELECT id FROM media WHERE id = ?", (media_id,)) is None


async def test_apply_records_returns_the_surviving_id(db: Any, settings: Any, http: Any, media_id: int) -> None:
    await store_record(
        db,
        media_id,
        ProviderRecord(provider="dnb", external_id="d2", status=ProviderStatus.OK, title="Titel"),
        confirmed=True,
    )
    assert await apply_records(db, settings, http, media_id) == media_id


async def test_a_confirmed_shop_answer_fills_a_gap_but_never_outranks_a_catalogue() -> None:
    """Shops are appended to every field's precedence, not listed in it: they
    answer price and cover, and a person's click must not make them the
    authority on title or publisher."""
    dnb = ProviderRecord(
        provider="dnb",
        external_id="d1",
        status=ProviderStatus.OK,
        title="Unterwerfung",
        authors=["Michel Houellebecq"],
        isbn13="9783462056344",
        publisher="Kiepenheuer & Witsch",
    )
    shop = ProviderRecord(
        provider="buchkatalog",
        external_id="4099276460851090106",
        status=ProviderStatus.OK,
        title="Unterwerfung (Taschenbuchausgabe)",
        authors=["M. Houellebecq"],
        publisher="Buchkatalog GmbH",
        description="Nur der Shop kennt diesen Text.",
        cover_source_url="https://www.buchkatalog.de/c/1.jpg",
    )
    merged = merge_records([dnb, shop])
    assert merged["title"] == "Unterwerfung"
    assert merged["publisher"] == "Kiepenheuer & Witsch"
    # The gap no catalogue stated is exactly what the pick is for.
    assert merged["description"] == "Nur der Shop kennt diesen Text."
    assert merged["cover_source_url"] == "https://www.buchkatalog.de/c/1.jpg"
    assert "buchkatalog" in FIELD_PRECEDENCE["title"]


def test_a_shop_isbn_fills_the_gap_but_a_catalogue_isbn_wins() -> None:
    """The apply path's whole value is the ISBN a pick carries: the merged
    isbn13 is what `adopt_isbn` moves the row under, and a shop that answered
    a work the catalogues never named must be able to give it its ISBN —
    while a catalogue that stated one still outranks the shop."""
    shop = ProviderRecord(
        provider="buchkatalog",
        external_id="4099276460851090106",
        status=ProviderStatus.OK,
        title="Unterwerfung",
        isbn13="9783832163594",
    )
    only_shop = merge_records([shop])
    assert only_shop["isbn13"] == "9783832163594"

    dnb = ProviderRecord(
        provider="dnb",
        external_id="d1",
        status=ProviderStatus.OK,
        isbn13="9783462056344",
    )
    both = merge_records([dnb, shop])
    assert both["isbn13"] == "9783462056344"


# -- endpoints ---------------------------------------------------------------


def _sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: ") :]) for line in text.splitlines() if line.startswith("data: ")]


async def test_the_search_stream_reports_each_source_as_it_answers(client: Any, settings: Any, media_id: int) -> None:
    settings.metadata_providers = ["dnb", "blocked", "empty"]

    response = await client.get(f"/api/media/{media_id}/identify/search", params={"q": "Unterwerfung"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse(response.text)

    begin = events[0]
    assert begin["event"] == "begin"
    assert [p["name"] for p in begin["providers"]] == ["dnb", "blocked", "empty"]

    statuses = {e["provider"]: e for e in events if e["event"] == "status"}
    assert statuses["dnb"]["status"] == "found"
    assert statuses["blocked"]["status"] == "blocked"
    assert statuses["empty"]["status"] == "none"
    assert events[-1] == {"event": "done"}

    # The card is server-rendered markup, not a DOM the client has to build.
    card = next(e for e in events if e["event"] == "card")
    assert "Unterwerfung" in card["html"]
    assert 'data-external-id="a1"' in card["html"]
    assert "22,99" in card["html"]


async def test_an_unknown_work_is_a_404_not_an_empty_stream(client: Any, settings: Any) -> None:
    settings.metadata_providers = ["dnb"]
    response = await client.get("/api/media/424242/identify/search", params={"q": "x"})
    assert response.status_code == 404


async def test_apply_writes_the_picked_record_and_names_the_row(
    client: Any, db: Any, settings: Any, media_id: int
) -> None:
    """The dialog posts provider + external id only; everything else, the
    price included, has to arrive from the provider."""
    settings.metadata_providers = ["dnb"]

    response = await client.post(f"/api/media/{media_id}/identify/apply", data={"provider": "dnb", "external_id": "a1"})
    assert response.status_code == 200
    assert response.json() == {"media_id": media_id}
    assert response.headers["hx-redirect"] == f"/media/{media_id}"

    media = await db.fetch_one("SELECT effective_price_cents FROM media WHERE id = ?", (media_id,))
    assert media is not None and media["effective_price_cents"] == 2299


async def test_apply_refuses_a_half_told_choice(client: Any, settings: Any, media_id: int) -> None:
    settings.metadata_providers = ["dnb"]
    response = await client.post(f"/api/media/{media_id}/identify/apply", data={"provider": "dnb"})
    assert response.status_code == 400


async def test_apply_refuses_a_vanished_result(client: Any, settings: Any, media_id: int) -> None:
    settings.metadata_providers = ["empty"]
    response = await client.post(
        f"/api/media/{media_id}/identify/apply", data={"provider": "empty", "external_id": "a1"}
    )
    assert response.status_code == 422


async def test_apply_names_the_work_that_survives_an_isbn_merge(
    client: Any, db: Any, settings: Any, media_id: int
) -> None:
    settings.metadata_providers = ["dnb"]
    other = await _seed_other(db, "other2")
    response = await client.post(f"/api/media/{media_id}/identify/apply", data={"provider": "dnb", "external_id": "a1"})
    assert response.json() == {"media_id": other}
    assert response.headers["hx-redirect"] == f"/media/{other}"


async def test_the_button_only_appears_when_a_source_can_answer(client: Any, settings: Any, media_id: int) -> None:
    settings.metadata_providers = ["dnb"]
    page = await client.get(f"/media/{media_id}")
    assert 'id="identify-open"' in page.text
    # The dialog's own search box starts out holding what the row is called.
    assert "ohne titel" in page.text

    settings.metadata_providers = []
    page = await client.get(f"/media/{media_id}")
    assert 'id="identify-open"' not in page.text
