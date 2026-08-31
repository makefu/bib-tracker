"""What a poll is allowed to write to the history, and what it is not.

These are the tests that protect the lending history from a broken scraper.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from bib_tracker.db.connection import Database
from bib_tracker.services import PollInProgressError, PollService
from tests.conftest import library_fixture

BASE_URL = "http://opac.test"


def _mock(checkouts: str, *, login_page: str | None = None) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=login_page or checkouts)
    )
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-detail.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_checkouts.html"))
    )


async def _run(db: Database, run_id: int) -> dict:
    row = await db.fetch_one("SELECT * FROM poll_runs WHERE id = ?", (run_id,))
    assert row is not None
    return dict(row)


def _only_first_loan(html: str) -> str:
    """Keep just the first checkout row, so three of four loans vanish."""
    lines = html.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == "<tbody>")
    end = next(i for i, line in enumerate(lines) if line.strip() == "</tbody>")
    body = "".join(lines[start + 1 : end])
    rows = re.findall(r"[ \t]*<tr[^>]*>.*?</tr>\n", body, re.S)
    assert len(rows) == 4
    return "".join(lines[: start + 1]) + rows[0] + "".join(lines[end:])


async def _snapshot_count(db: Database, run_id: int) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS n FROM snapshot_items WHERE run_id = ?", (run_id,))
    return int(row["n"])  # type: ignore[index]


@respx.mock
async def test_a_successful_poll_records_its_observations(poll_service: PollService, db: Database) -> None:
    _mock(library_fixture("remseck_checkouts.html"))

    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "success"
    assert run["loan_count"] == 4
    assert run["finished_at"] is not None
    assert await _snapshot_count(db, run_id) == 4


@respx.mock
async def test_snapshot_items_carry_a_stable_copy_key(poll_service: PollService, db: Database) -> None:
    _mock(library_fixture("remseck_checkouts.html"))

    first = await poll_service.poll("remseck")
    second = await poll_service.poll("remseck")

    keys_first = {
        r["copy_key"] for r in await db.fetch_all("SELECT copy_key FROM snapshot_items WHERE run_id = ?", (first,))
    }
    keys_second = {
        r["copy_key"] for r in await db.fetch_all("SELECT copy_key FROM snapshot_items WHERE run_id = ?", (second,))
    }
    assert keys_first == keys_second
    assert len(keys_first) == 4


@respx.mock
async def test_an_auth_failure_records_no_snapshot(poll_service: PollService, db: Database) -> None:
    """The heart of it: bad credentials must not look like an empty account."""
    _mock(library_fixture("remseck_checkouts.html"))
    await poll_service.poll("remseck")

    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_login.html"))
    )
    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "auth_error"
    assert run["loan_count"] is None
    assert await _snapshot_count(db, run_id) == 0


@respx.mock
async def test_an_expired_session_records_no_snapshot(poll_service: PollService, db: Database) -> None:
    _mock(library_fixture("remseck_login.html"), login_page=library_fixture("remseck_checkouts.html"))

    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "parse_error"
    assert await _snapshot_count(db, run_id) == 0


@respx.mock
async def test_a_network_failure_records_no_snapshot(poll_service: PollService, db: Database) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(side_effect=httpx.ConnectError("no route"))

    run_id = await poll_service.poll("remseck")

    assert (await _run(db, run_id))["status"] == "network_error"
    assert await _snapshot_count(db, run_id) == 0


@respx.mock
async def test_a_missing_password_file_is_recorded_not_raised(poll_service: PollService, db: Database) -> None:
    poll_service.accounts[0].password_file.unlink()

    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "internal_error"
    assert "password" in (run["error_message"] or "").lower() or run["error_kind"] == "FileNotFoundError"


@respx.mock
async def test_a_sudden_emptiness_is_held_back_as_suspect(poll_service: PollService, db: Database) -> None:
    """Both scrapers return [] when their selector misses, so one empty result
    is not evidence that everything was returned."""
    _mock(library_fixture("remseck_checkouts.html"))
    first = await poll_service.poll("remseck")
    await db.fetch_one("SELECT 1")
    async with db.write() as w:
        await w.execute("UPDATE poll_runs SET reconciled = 1 WHERE id = ?", (first,))

    _mock(library_fixture("remseck_no_checkouts.html"))
    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "suspect"
    assert "vanished" in run["suspect_reason"]
    assert run["reconciled"] == 0
    # Nothing was borrowed, so there is nothing to store either way.
    assert await _snapshot_count(db, run_id) == 0


@respx.mock
async def test_a_confirmed_emptiness_is_accepted(poll_service: PollService, db: Database) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    first = await poll_service.poll("remseck")
    async with db.write() as w:
        await w.execute("UPDATE poll_runs SET reconciled = 1 WHERE id = ?", (first,))

    _mock(library_fixture("remseck_no_checkouts.html"))
    await poll_service.poll("remseck")
    second = await poll_service.poll("remseck")

    assert (await _run(db, second))["status"] == "success"


@respx.mock
async def test_returning_most_of_a_pile_at_once_is_accepted(poll_service: PollService, db: Database) -> None:
    """Three of four books going back in one trip is ordinary household
    behaviour. Flagging it would make the confirmation prompt into noise, and
    a prompt people click through blindly protects nothing."""
    _mock(library_fixture("remseck_checkouts.html"))
    first = await poll_service.poll("remseck")
    async with db.write() as w:
        await w.execute("UPDATE poll_runs SET reconciled = 1 WHERE id = ?", (first,))

    _mock(_only_first_loan(library_fixture("remseck_checkouts.html")))
    run_id = await poll_service.poll("remseck")

    assert (await _run(db, run_id))["status"] == "success"


@respx.mock
async def test_a_bulk_disappearance_is_held_back_at_the_configured_ratio(
    poll_service: PollService, db: Database
) -> None:
    """Above the configured share, a mass disappearance is treated as a likely
    parser break: the observation is kept, its promotion to history waits."""
    poll_service._settings.suspect_drop_ratio = 0.7

    _mock(library_fixture("remseck_checkouts.html"))
    first = await poll_service.poll("remseck")
    async with db.write() as w:
        await w.execute("UPDATE poll_runs SET reconciled = 1 WHERE id = ?", (first,))

    _mock(_only_first_loan(library_fixture("remseck_checkouts.html")))
    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "suspect"
    assert "3 of 4" in run["suspect_reason"]
    assert run["reconciled"] == 0
    # The observation survives, so confirming it later needs no re-fetch.
    assert await _snapshot_count(db, run_id) == 1


@respx.mock
async def test_a_normal_return_is_not_suspect(poll_service: PollService, db: Database) -> None:
    """One item going back is the ordinary case and must not be held up."""
    _mock(library_fixture("remseck_checkouts.html"))
    first = await poll_service.poll("remseck")
    async with db.write() as w:
        await w.execute("UPDATE poll_runs SET reconciled = 1 WHERE id = ?", (first,))

    _mock(library_fixture("remseck_checkouts_returned.html"))
    run_id = await poll_service.poll("remseck")

    run = await _run(db, run_id)
    assert run["status"] == "success"
    assert run["loan_count"] == 3


@respx.mock
async def test_the_first_ever_poll_of_an_empty_account_is_not_suspect(poll_service: PollService, db: Database) -> None:
    _mock(library_fixture("remseck_no_checkouts.html"))

    run_id = await poll_service.poll("remseck")

    assert (await _run(db, run_id))["status"] == "success"


@respx.mock
async def test_concurrent_polls_of_one_account_are_refused(poll_service: PollService) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    lock = poll_service._locks["remseck"]
    await lock.acquire()
    try:
        with pytest.raises(PollInProgressError):
            await poll_service.poll("remseck")
    finally:
        lock.release()
