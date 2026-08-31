"""Matching, merging and pricing."""

from __future__ import annotations

import httpx
import pytest
import respx

from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata.base import MediaQuery, ProviderCandidate, ProviderRecord, ProviderStatus
from bib_tracker.metadata.matcher import AUTO_ACCEPT, NEEDS_CONFIRMATION, choose, score
from bib_tracker.metadata.merge import consensus_rating, merge_records, ratings
from bib_tracker.metadata.pricing import Price, PriceBasis, resolve_price, store_price
from tests.conftest import library_fixture

OPAC = "http://opac.test"


def _mock_opac() -> None:
    checkouts = library_fixture("remseck_checkouts.html")
    respx.post(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )


QUERY = MediaQuery(
    media_class=MediaClass.BOOK,
    title="Die unendliche Geschichte",
    author="Ende, Michael",
    published_year=1979,
)


def candidate(title: str, authors: list[str] | None = None, year: int | None = None, isbn: str | None = None):
    return ProviderCandidate(external_id="x", title=title, authors=authors or [], year=year, isbn13=isbn)


# -- matching ---------------------------------------------------------------


def test_an_exact_isbn_match_outranks_everything() -> None:
    query = MediaQuery(media_class=MediaClass.BOOK, title="Falscher Titel", isbn="9783522621885")
    assert score(query, candidate("Anderer Titel", isbn="9783522621885")) == 1.0


def test_the_same_book_under_a_different_name_order_matches() -> None:
    assert score(QUERY, candidate("Die unendliche Geschichte", ["Michael Ende"], 1979)) >= AUTO_ACCEPT


def test_a_different_book_by_the_same_author_does_not_match() -> None:
    assert score(QUERY, candidate("Momo", ["Michael Ende"], 1973)) < NEEDS_CONFIRMATION


def test_a_contradicting_year_is_a_hard_gate() -> None:
    """Same title, wrong decade: an edition drifts a year or two, not thirty."""
    assert score(QUERY, candidate("Die unendliche Geschichte", ["Michael Ende"], 2015)) == 0.0


def test_a_companion_volume_by_another_author_is_rejected() -> None:
    """Same series, different book and different author: close enough to be
    tempting, which is exactly why the author has to count against it."""
    best, _ = choose(QUERY, [candidate("Die unendliche Geschichte - Das Phantasien-Lexikon", ["Roman Hocke"], 1979)])
    assert best is None


def test_a_near_miss_is_offered_for_confirmation_rather_than_guessed() -> None:
    """A wrong match poisons a work's cover, price and rating for good, so the
    middle band asks instead of deciding."""
    best, needs_confirmation = choose(
        QUERY, [candidate("Die unendliche Geschichte, illustrierte Ausgabe", ["Michael Ende"], 1979)]
    )
    assert best is not None
    assert needs_confirmation is True
    assert NEEDS_CONFIRMATION <= best.score < AUTO_ACCEPT


def test_nothing_plausible_yields_no_match() -> None:
    best, needs_confirmation = choose(QUERY, [candidate("Der Herr der Ringe", ["Tolkien"], 1954)])
    assert best is None
    assert needs_confirmation is False


def test_a_missing_author_neither_helps_nor_hurts() -> None:
    query = MediaQuery(media_class=MediaClass.BOOK, title="Momo")
    assert score(query, candidate("Momo")) >= AUTO_ACCEPT


# -- merging ----------------------------------------------------------------


def _record(provider: str, **kwargs) -> ProviderRecord:
    return ProviderRecord(provider=provider, external_id="1", status=ProviderStatus.OK, **kwargs)


def test_the_national_library_wins_on_bibliographic_identity() -> None:
    merged = merge_records(
        [
            _record("googlebooks", isbn13="9999999999999", publisher="Google"),
            _record("dnb", isbn13="9783522621885", publisher="Thienemann"),
        ]
    )
    assert merged["isbn13"] == "9783522621885"
    assert merged["publisher"] == "Thienemann"


def test_google_wins_on_page_count_and_description() -> None:
    merged = merge_records(
        [
            _record("dnb", page_count=100),
            _record("googlebooks", page_count=480, description="Bastian findet ein Buch."),
        ]
    )
    assert merged["page_count"] == 480
    assert merged["description"].startswith("Bastian")


def test_a_failed_provider_contributes_nothing() -> None:
    merged = merge_records(
        [
            ProviderRecord(provider="dnb", external_id="1", status=ProviderStatus.ERROR, publisher="Falsch"),
            _record("googlebooks", publisher="Thienemann"),
        ]
    )
    assert merged["publisher"] == "Thienemann"


def test_ratings_are_never_merged_into_one_number() -> None:
    """4.1 out of 5 and 7.8 out of 10 are different claims; averaging them
    produces a figure nobody stated."""
    records = [
        _record("openlibrary", rating_value=4.1, rating_scale=5.0, rating_count=60),
        _record("bgg", rating_value=7.8, rating_scale=10.0, rating_count=94000),
    ]
    merged = merge_records(records)
    assert "rating_value" not in merged

    listed = ratings(records)
    assert {r["provider"] for r in listed} == {"openlibrary", "bgg"}
    assert [r["scale"] for r in listed if r["provider"] == "bgg"] == [10.0]


def test_the_consensus_rating_is_normalised_and_count_weighted() -> None:
    value = consensus_rating(
        [
            _record("openlibrary", rating_value=5.0, rating_scale=5.0, rating_count=1),
            _record("bgg", rating_value=5.0, rating_scale=10.0, rating_count=99),
        ]
    )
    assert value is not None
    # Dominated by the far more numerous BGG votes at half marks.
    assert 0.5 < value < 0.56


def test_no_ratings_means_no_consensus() -> None:
    assert consensus_rating([_record("dnb")]) is None


# -- pricing ----------------------------------------------------------------


@pytest.fixture
async def priced_media(db):
    async with db.write() as w:
        media_id = await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, first_seen_at, last_seen_at)
            VALUES ('k', 'book', 'Momo', datetime('now'), datetime('now'))
            """
        )
    return media_id


async def test_without_any_source_the_class_default_is_used_and_marked(db, settings, priced_media) -> None:
    price = await resolve_price(db, settings, priced_media, "book")

    assert price.cents == 1500
    assert price.basis is PriceBasis.DEFAULT
    assert price.is_estimate is True


async def test_a_provider_price_beats_the_default(db, settings, priced_media) -> None:
    price = await resolve_price(db, settings, priced_media, "book", provider_price_cents=2400)

    assert price.cents == 2400
    assert price.basis is PriceBasis.PROVIDER
    assert price.is_estimate is False


async def test_a_price_you_typed_beats_everything(db, settings, priced_media) -> None:
    """You can see the book. No lookup outranks that."""
    await store_price(db, priced_media, Price(1899, PriceBasis.MANUAL), source="manual")

    price = await resolve_price(db, settings, priced_media, "book", provider_price_cents=2400)

    assert price.cents == 1899
    assert price.basis is PriceBasis.MANUAL
    assert price.is_estimate is False


async def test_storing_a_price_records_its_basis_on_the_work(db, settings, priced_media) -> None:
    await store_price(db, priced_media, Price(3500, PriceBasis.DEFAULT), source="default_by_class")

    row = await db.fetch_one("SELECT effective_price_cents, price_basis FROM media WHERE id = ?", (priced_media,))
    assert row["effective_price_cents"] == 3500
    assert row["price_basis"] == "default_by_class"


async def test_game_and_book_defaults_differ(db, settings, priced_media) -> None:
    book = await resolve_price(db, settings, priced_media, "book")
    game = await resolve_price(db, settings, priced_media, "game")
    assert game.cents > book.cents


# -- manual price, through the interface ------------------------------------


@respx.mock
async def test_a_price_typed_in_the_interface_wins_and_says_so(api) -> None:
    """The basis is always shown, so an estimate is never mistaken for a fact."""
    _mock_opac()
    await api.post("/api/accounts/remseck/poll")
    media_id = (await api.get("/api/history")).json()["loans"][0]["media_id"]

    before = (await api.get(f"/media/{media_id}")).text
    assert "geschätzt" in before or "unbekannt" in before

    response = await api.post(f"/api/media/{media_id}/price", data={"price": "18,99"})

    assert response.status_code == 200
    assert "von dir eingetragen" in response.text
    assert "18,99" in response.text


@respx.mock
async def test_a_nonsense_price_is_refused(api) -> None:
    _mock_opac()
    await api.post("/api/accounts/remseck/poll")
    media_id = (await api.get("/api/history")).json()["loans"][0]["media_id"]

    assert (await api.post(f"/api/media/{media_id}/price", data={"price": "gratis"})).status_code == 400
    assert (await api.post(f"/api/media/{media_id}/price", data={"price": "-5"})).status_code == 400
