"""Back-off after a rate limit, and the per-copy detail request budget.

The Stuttgart aDIS firewall answers 403 for hours after a burst; these tests
pin the two mechanisms that keep the poll from living inside that block: the
scheduler deferring a backed-off account, and the detail ledger turning a
request-per-loan into a request-per-copy-per-month.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from bib_tracker.db.connection import Database
from bib_tracker.services import PollService
from tests.conftest import library_fixture

BASE_URL = "http://opac.test"


def _mock_opac(checkouts: str) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )


def _mock_details(hits: list[str]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, html=library_fixture("remseck_detail.html"))

    respx.get(url__startswith=f"{BASE_URL}/cgi-bin/koha/opac-detail.pl").mock(side_effect=respond)


async def _account_row(db: Database) -> dict:
    row = await db.fetch_one("SELECT * FROM accounts WHERE name = 'remseck'")
    assert row is not None
    return dict(row)


async def _runs(db: Database) -> list[dict]:
    rows = await db.fetch_all("SELECT * FROM poll_runs ORDER BY id")
    return [dict(row) for row in rows]


# -- the rate-limit classification ------------------------------------------


@respx.mock
async def test_a_forbidden_opac_is_a_rate_limit_not_a_network_error(poll_service: PollService, db: Database) -> None:
    """'Back off for hours' and 'the cable is unplugged' must not share a status:
    one argues for silence, the other for a retry on the next tick."""
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))

    run_id = await poll_service.poll("remseck")

    run = next(row for row in await _runs(db) if row["id"] == run_id)
    assert run["status"] == "rate_limited"
    assert run["error_kind"] == "RateLimitedError"


@respx.mock
async def test_a_rate_limited_poll_opens_the_backoff_window(poll_service: PollService, db: Database) -> None:
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))

    await poll_service.poll("remseck")

    row = await _account_row(db)
    assert row["backoff_failures"] == 1
    until = datetime.fromisoformat(row["backoff_until"])
    # base is 360 minutes; allow for the poll's own seconds of work
    assert datetime.now(UTC) + timedelta(minutes=350) < until < datetime.now(UTC) + timedelta(minutes=370)


@respx.mock
async def test_consecutive_rate_limits_double_the_window_and_the_cap_holds(
    poll_service: PollService, db: Database
) -> None:
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))

    await poll_service.poll("remseck")
    await poll_service.poll("remseck")
    assert (await _account_row(db))["backoff_failures"] == 2
    second = datetime.fromisoformat((await _account_row(db))["backoff_until"])
    assert datetime.now(UTC) + timedelta(minutes=710) < second < datetime.now(UTC) + timedelta(minutes=730)

    for _ in range(6):  # 360 * 2^6 minutes is over the 24h cap
        await poll_service.poll("remseck")
    row = await _account_row(db)
    assert row["backoff_failures"] == 8
    capped = datetime.fromisoformat(row["backoff_until"])
    assert capped < datetime.now(UTC) + timedelta(hours=24, minutes=10)


@respx.mock
async def test_a_retry_after_hint_overrides_the_exponential_schedule(
    poll_service: PollService, db: Database, settings
) -> None:
    """When the server states how long to wait, that is the floor — a
    exponential ladder that starts below it just wastes the next two polls."""
    settings.poll_backoff_base_minutes = 5
    settings.poll_backoff_max_hours = 1
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(429, text="slow down", headers={"Retry-After": "7200"})
    )

    await poll_service.poll("remseck")

    until = datetime.fromisoformat((await _account_row(db))["backoff_until"])
    assert datetime.now(UTC) + timedelta(minutes=115) < until < datetime.now(UTC) + timedelta(minutes=125)


@respx.mock
async def test_a_successful_poll_clears_the_backoff(poll_service: PollService, db: Database) -> None:
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))
    await poll_service.poll("remseck")
    assert (await _account_row(db))["backoff_until"] is not None

    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_checkouts.html"))
    )
    await poll_service.poll("remseck")

    row = await _account_row(db)
    assert row["backoff_failures"] == 0
    assert row["backoff_until"] is None


# -- the gate: a backed-off account stays silent to the schedule ------------


@respx.mock
async def test_the_schedule_defers_a_backed_off_account_but_a_manual_poll_runs(
    poll_service: PollService, db: Database
) -> None:
    """The point of the ledger: the next scheduled tick must not knock again
    while the firewall is still holding the door — but the operator's button
    is never gated."""
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))
    await poll_service.poll("remseck")
    runs_before = len(await _runs(db))

    deferred = await poll_service.poll("remseck", trigger="schedule")
    startup = await poll_service.poll("remseck", trigger="startup")
    assert deferred is None
    assert startup is None
    assert len(await _runs(db)) == runs_before

    manual = await poll_service.poll("remseck", trigger="manual")
    assert manual is not None
    assert len(await _runs(db)) == runs_before + 1


@respx.mock
async def test_an_expired_backoff_window_lets_the_schedule_poll_again(poll_service: PollService, db: Database) -> None:
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))
    await poll_service.poll("remseck")

    async with db.write() as w:
        await w.execute("UPDATE accounts SET backoff_until = datetime('now', '-1 minute') WHERE name = 'remseck'")

    assert await poll_service.poll("remseck", trigger="schedule") is not None


@respx.mock
async def test_a_rate_limited_poll_records_no_snapshot(poll_service: PollService, db: Database) -> None:
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(403, text="Forbidden"))

    run_id = await poll_service.poll("remseck")

    row = await db.fetch_one("SELECT COUNT(*) AS n FROM snapshot_items WHERE run_id = ?", (run_id,))
    assert row is not None and int(row["n"]) == 0


# -- the detail ledger: one request per copy, not one per loan per poll -----


@respx.mock
async def test_detail_pages_are_fetched_once_per_copy_not_per_poll(
    poll_service: PollService, db: Database, settings
) -> None:
    """Four loans used to mean four detail requests per poll. The ledger makes
    it four per details_retry_days, and the second poll must still show the
    enrichment — from the ledger, not from a request."""
    settings.metadata_enabled = True
    _mock_opac(library_fixture("remseck_checkouts.html"))
    hits: list[str] = []
    _mock_details(hits)

    async def isbns(run_id: int) -> dict[str, str | None]:
        rows = await db.fetch_all("SELECT title, isbn FROM snapshot_items WHERE run_id = ?", (run_id,))
        return {row["title"]: row["isbn"] for row in rows}

    first = await poll_service.poll("remseck")
    assert len(hits) == 4
    assert any(value == "9783522621885" for value in (await isbns(first)).values())

    second = await poll_service.poll("remseck")
    assert len(hits) == 4  # nothing new was asked for
    assert await isbns(second) == await isbns(first)  # but the enrichment is still there


@respx.mock
async def test_a_failed_detail_lookup_is_cached_as_nothing(poll_service: PollService, db: Database, settings) -> None:
    """A title the catalogue has no jacket for must not cost a request on every
    poll: 'nothing' is an answer, recorded so it is not asked again."""
    settings.metadata_enabled = True
    _mock_opac(library_fixture("remseck_checkouts.html"))
    hits: list[str] = []
    respx.get(url__startswith=f"{BASE_URL}/cgi-bin/koha/opac-detail.pl").mock(side_effect=httpx.ConnectError("down"))

    await poll_service.poll("remseck")
    rows = await db.fetch_all("SELECT copy_key, cover_url, isbn FROM detail_lookups")
    assert len(rows) == 4
    # the detail page said nothing: no ISBN was invented. A cover may still
    # be on the row — that is the one the listing itself carried.
    assert all(row["isbn"] is None for row in rows)

    await poll_service.poll("remseck")
    assert len(hits) == 0


@respx.mock
async def test_an_expired_detail_ledger_refetches(poll_service: PollService, db: Database, settings) -> None:
    settings.metadata_enabled = True
    settings.details_retry_days = 30
    _mock_opac(library_fixture("remseck_checkouts.html"))
    hits: list[str] = []
    _mock_details(hits)

    await poll_service.poll("remseck")
    assert len(hits) == 4

    async with db.write() as w:
        await w.execute("UPDATE detail_lookups SET fetched_at = datetime('now', '-31 days')")

    await poll_service.poll("remseck")
    assert len(hits) == 8


@respx.mock
async def test_a_rate_limit_during_details_still_opens_the_backoff(
    poll_service: PollService, db: Database, settings
) -> None:
    """A 403 from a detail page is the same firewall blocking the same site —
    it must not be swallowed as 'one loan's page is broken'."""
    settings.metadata_enabled = True
    _mock_opac(library_fixture("remseck_checkouts.html"))
    respx.get(url__startswith=f"{BASE_URL}/cgi-bin/koha/opac-detail.pl").mock(
        return_value=httpx.Response(403, text="Request forbidden by administrative rules")
    )

    run_id = await poll_service.poll("remseck")

    run = next(row for row in await _runs(db) if row["id"] == run_id)
    assert run["status"] == "rate_limited"
    assert (await _account_row(db))["backoff_failures"] == 1
