"""Entering a loan the tracker never saw."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from bib_tracker.db import queries
from bib_tracker.library.manual import ManualLoan, add_past_loan
from bib_tracker.library.media_class import MediaClass
from bib_tracker.library.reconcile import rebuild_history
from tests.conftest import library_fixture

OPAC = "http://opac.test"


def _mock_opac(fixture: str = "remseck_checkouts.html") -> None:
    html = library_fixture(fixture)
    respx.post(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=html))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=html))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )


@pytest.fixture
async def seeded(db, settings, account_config):
    await queries.sync_accounts(db, [account_config])
    return account_config


ENTRY = ManualLoan(
    account="remseck",
    title="Der Vorleser",
    author="Schlink, Bernhard",
    isbn="9783257229530",
    lend_date=date(2025, 3, 1),
    return_date=date(2025, 3, 22),
)


async def test_a_past_loan_appears_in_the_history(db, settings, seeded) -> None:
    await add_past_loan(db, settings, ENTRY)

    rows = await db.fetch_all(
        "SELECT m.title, m.author, m.isbn13, l.state, l.lend_date, l.return_date"
        " FROM loans l JOIN media m ON m.id = l.media_id"
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "Der Vorleser"
    assert rows[0]["author"] == "Schlink, Bernhard"
    assert rows[0]["isbn13"] == "9783257229530"
    assert rows[0]["state"] == "returned"
    assert rows[0]["lend_date"] == "2025-03-01"
    assert rows[0]["return_date"] == "2025-03-22"


async def test_typed_dates_are_recorded_as_typed_not_guessed(db, settings, seeded) -> None:
    await add_past_loan(db, settings, ENTRY)

    row = await db.fetch_one("SELECT lend_date_source, return_date_source FROM loans")
    assert row["lend_date_source"] == "manual"
    assert row["return_date_source"] == "manual"


async def test_an_ongoing_past_loan_stays_open(db, settings, seeded) -> None:
    await add_past_loan(
        db,
        settings,
        ManualLoan(account="remseck", title="Momo", lend_date=date(2025, 5, 1)),
    )

    row = await db.fetch_one("SELECT state, return_date FROM loans")
    assert row["state"] == "open"
    assert row["return_date"] is None


async def test_a_hand_entered_loan_survives_a_rebuild(db, settings, seeded) -> None:
    """It lives in the observation layer, so recomputing history keeps it."""
    await add_past_loan(db, settings, ENTRY)

    await rebuild_history(db, settings)

    rows = await db.fetch_all(
        "SELECT m.title, l.lend_date, l.return_date, l.lend_date_source FROM loans l JOIN media m ON m.id = l.media_id"
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "Der Vorleser"
    assert rows[0]["lend_date"] == "2025-03-01"
    assert rows[0]["lend_date_source"] == "manual"


@respx.mock
async def test_adding_a_past_loan_does_not_return_everything_else(db, settings, seeded, poll_service) -> None:
    """The obvious way to get this wrong: a one-item synthetic snapshot looks
    to the reconciler exactly like an account that was emptied."""
    _mock_opac()
    await poll_service.poll("remseck")
    open_before = await db.fetch_one("SELECT COUNT(*) AS n FROM loans WHERE state = 'open'")
    assert open_before["n"] == 4

    await add_past_loan(db, settings, ENTRY)

    open_after = await db.fetch_one("SELECT COUNT(*) AS n FROM loans WHERE state = 'open'")
    assert open_after["n"] == 4
    total = await db.fetch_one("SELECT COUNT(*) AS n FROM loans")
    assert total["n"] == 5


async def test_the_work_is_shared_with_a_later_real_borrow(db, settings, seeded) -> None:
    """Entering a past loan of something later borrowed again must not create
    a second copy of the same work."""
    await add_past_loan(db, settings, ENTRY)
    await add_past_loan(
        db,
        settings,
        ManualLoan(
            account="remseck",
            title="Der Vorleser",
            author="Schlink, Bernhard",
            lend_date=date(2025, 9, 1),
            return_date=date(2025, 9, 20),
        ),
    )

    media = await db.fetch_all("SELECT id FROM media")
    loans = await db.fetch_all("SELECT id FROM loans")
    assert len(media) == 1
    assert len(loans) == 2


async def test_media_class_drives_the_default_price(db, settings, seeded) -> None:
    await add_past_loan(
        db,
        settings,
        ManualLoan(
            account="remseck",
            title="Azul",
            media_class=MediaClass.GAME,
            lend_date=date(2025, 4, 1),
            return_date=date(2025, 4, 15),
        ),
    )

    row = await db.fetch_one("SELECT media_class FROM media")
    assert row["media_class"] == "game"


async def test_an_unknown_account_is_refused(db, settings, seeded) -> None:
    with pytest.raises(KeyError):
        await add_past_loan(db, settings, ManualLoan(account="nirgendwo", title="X", lend_date=date(2025, 1, 1)))


# -- through the interface ---------------------------------------------------


@respx.mock
async def test_the_form_records_a_past_loan(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/history/add",
        data={
            "account": "remseck",
            "title": "Der Vorleser",
            "author": "Schlink, Bernhard",
            "isbn": "978-3-257-22953-0",
            "media_class": "book",
            "lend_date": "2025-03-01",
            "return_date": "2025-03-22",
        },
    )

    assert response.status_code == 200
    history = (await api.get("/api/history")).json()
    assert history["count"] == 1
    entry = history["loans"][0]
    assert entry["title"] == "Der Vorleser"
    assert entry["lend_date"] == "2025-03-01"
    assert entry["lend_date_source"] == "manual"


@respx.mock
async def test_a_return_before_the_loan_is_refused(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/history/add",
        data={
            "account": "remseck",
            "title": "Momo",
            "lend_date": "2025-03-22",
            "return_date": "2025-03-01",
        },
    )
    assert response.status_code == 400


@respx.mock
async def test_an_isbn_lookup_shows_the_match_before_saving(api: httpx.AsyncClient) -> None:
    """A wrong match is caught by the person who can tell, not stored first."""
    respx.get(url__regex=r"https://services\.dnb\.de/.*").mock(
        return_value=httpx.Response(
            200,
            text=(__import__("pathlib").Path("tests/fixtures/providers/dnb_sru.xml").read_text(encoding="utf-8")),
        )
    )

    response = await api.post("/history/add/lookup", data={"isbn": "9783126741316"})

    assert response.status_code == 200
    assert "Die unendliche Geschichte" in response.text
    assert "dnb" in response.text


@respx.mock
async def test_an_unknown_isbn_says_so_rather_than_guessing(api: httpx.AsyncClient) -> None:
    respx.get(url__regex=r"https://services\.dnb\.de/.*").mock(
        return_value=httpx.Response(200, text="<searchRetrieveResponse/>")
    )
    respx.get(url__regex=r"https://openlibrary\.org/.*").mock(return_value=httpx.Response(200, text="{}"))

    response = await api.post("/history/add/lookup", data={"isbn": "9999999999999"})

    assert "Nichts gefunden" in response.text


async def test_the_add_page_renders(client: httpx.AsyncClient) -> None:
    body = (await client.get("/history/add")).text
    assert "Ausleihe eintragen" in body
    assert 'name="isbn"' in body
