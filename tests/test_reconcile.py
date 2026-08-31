"""Deriving lending history from successive snapshots."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from bib_tracker.config import AccountConfig, Settings
from bib_tracker.db import queries
from bib_tracker.db.connection import Database
from bib_tracker.library.identity import copy_key
from bib_tracker.library.reconcile import rebuild_history, reconcile_pending

ACCOUNT = AccountConfig(name="remseck", library_type="remseck", username="1")


def loan(
    title: str,
    *,
    item_id: str,
    due: str,
    times_renewed: int = 0,
    checkout_date: str | None = None,
    author: str | None = "Ende, Michael",
    media_type: str | None = "Buch",
) -> dict[str, Any]:
    """One serialised loan, shaped exactly as serialize_loan() emits it."""
    return {
        "title": title,
        "item_id": item_id,
        "author": author,
        "media_type": media_type,
        "due_date": due,
        "checkout_date": checkout_date,
        "times_renewed": times_renewed,
        "max_renewals": 3,
        "can_be_renewed": True,
        "call_number": None,
        "barcode": None,
        "publisher": None,
        "isbn": None,
        "cover_url": None,
        "library_branch": "Mediathek im KUBUS",
        "detail_url": None,
    }


async def record_poll(
    db: Database,
    loans: list[dict[str, Any]],
    *,
    at: datetime,
    account_id: int = 1,
) -> int:
    """Store one successful observation, as PollService would."""
    run_id = await queries.start_run(db, account_id, "schedule", at)
    items = [
        {
            "copy_key": copy_key(
                "remseck",
                title=item["title"],
                item_id=item["item_id"],
                author=item["author"],
                media_type=item["media_type"],
            ),
            "raw": item,
        }
        for item in loans
    ]
    await queries.finish_run(
        db,
        run_id,
        status="success",
        finished_at=at,
        duration_ms=10,
        loan_count=len(items),
        snapshot=items,
        account_id=account_id,
        observed_at=at,
    )
    return run_id


@pytest.fixture
async def account(db: Database) -> int:
    await queries.sync_accounts(db, [ACCOUNT])
    row = await queries.get_account(db, "remseck")
    assert row is not None
    return row.id


@pytest.fixture
def reconcile_settings(settings: Settings) -> Settings:
    return settings


async def loans_of(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all("SELECT l.*, m.title FROM loans l JOIN media m ON m.id = l.media_id ORDER BY m.title")
    return [dict(row) for row in rows]


DAY = timedelta(days=1)
T0 = datetime(2026, 3, 1, 8, 0)


async def test_a_new_item_opens_a_loan(db: Database, reconcile_settings: Settings, account: int) -> None:
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)

    report = await reconcile_pending(db, reconcile_settings)

    assert report.opened == 1
    (row,) = await loans_of(db)
    assert row["state"] == "open"
    assert row["title"] == "Momo"
    assert row["return_date"] is None


async def test_an_exact_checkout_date_is_used_as_is(db: Database, reconcile_settings: Settings, account: int) -> None:
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-03-29", checkout_date="2026-03-01")],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    (row,) = await loans_of(db)
    assert row["lend_date"] == "2026-03-01"
    assert row["lend_date_source"] == "exact"
    assert row["duration_uncertainty_days"] == 0


async def test_a_loan_running_before_tracking_started_is_marked_as_such(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    """A renewed loan gives nothing to work back from, and on the first poll
    there is no earlier sighting either -- so its start is simply unknown, and
    saying so keeps it out of the duration statistics."""
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-03-29", times_renewed=2)],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    (row,) = await loans_of(db)
    assert row["lend_date_source"] == "before_tracking"
    assert row["lend_date_earliest"] is None


async def test_a_later_arrival_is_bounded_by_the_previous_poll(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(
        db,
        [
            loan("Momo", item_id="1", due="2026-03-29"),
            # Due date is not a whole loan period away, so it cannot be derived.
            loan("Tschick", item_id="2", due="2026-03-20", times_renewed=1),
        ],
        at=T0 + 5 * DAY,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    rows = {row["title"]: row for row in await loans_of(db)}
    tschick = rows["Tschick"]
    assert tschick["lend_date_source"] == "first_seen"
    assert tschick["lend_date_earliest"] == T0.date().isoformat()
    assert tschick["lend_date_latest"] == (T0 + 5 * DAY).date().isoformat()


async def test_a_due_date_one_loan_period_out_gives_a_better_estimate(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    """A never-renewed book due in 28 days started today, whatever time we
    happened to look."""
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    borrowed_on = (T0 + 3 * DAY).date()
    await record_poll(
        db,
        [
            loan("Momo", item_id="1", due="2026-03-29"),
            loan("Krabat", item_id="9", due=(borrowed_on + timedelta(days=28)).isoformat()),
        ],
        at=T0 + 5 * DAY,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    krabat = {row["title"]: row for row in await loans_of(db)}["Krabat"]
    assert krabat["lend_date_source"] == "due_minus_period"
    assert krabat["lend_date"] == borrowed_on.isoformat()


async def test_a_vanished_item_is_returned_with_bounds(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-03-29"), loan("Tschick", item_id="2", due="2026-03-29")],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0 + 4 * DAY, account_id=account)
    report = await reconcile_pending(db, reconcile_settings)

    assert report.closed == 1
    rows = {row["title"]: row for row in await loans_of(db)}
    tschick = rows["Tschick"]
    assert tschick["state"] == "returned"
    # Conservative: a long gap between polls must not inflate the duration.
    assert tschick["return_date"] == T0.date().isoformat()
    assert tschick["return_date_earliest"] == T0.date().isoformat()
    assert tschick["return_date_latest"] == (T0 + 4 * DAY).date().isoformat()
    assert rows["Momo"]["state"] == "open"


async def test_a_renewal_is_recorded_and_the_due_date_follows(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-04-26", times_renewed=1)],
        at=T0 + 20 * DAY,
        account_id=account,
    )
    report = await reconcile_pending(db, reconcile_settings)

    assert report.renewed == 1
    (row,) = await loans_of(db)
    assert row["times_renewed"] == 1
    assert row["last_due_date"] == "2026-04-26"
    assert row["first_due_date"] == "2026-03-29"

    renewals = await db.fetch_all("SELECT * FROM renewals")
    assert len(renewals) == 1
    assert renewals[0]["due_date_before"] == "2026-03-29"
    assert renewals[0]["due_date_after"] == "2026-04-26"


async def test_being_overdue_is_latched_even_after_a_renewal(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    """Renewing moves the due date. Recomputing overdue-ness from the current
    due date afterwards would erase the fact that it ever ran late."""
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-01")], at=T0 + 3 * DAY, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-05-01", times_renewed=1)],
        at=T0 + 10 * DAY,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    (row,) = await loans_of(db)
    assert row["was_overdue"] == 1
    assert row["max_overdue_days"] == 3


async def test_a_flapping_item_is_one_loan_not_two(db: Database, reconcile_settings: Settings, account: int) -> None:
    """One missed poll must not split a loan in half."""
    momo = loan("Momo", item_id="1", due="2026-03-29")
    await record_poll(db, [momo], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(db, [], at=T0 + DAY, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(db, [momo], at=T0 + 2 * DAY, account_id=account)
    report = await reconcile_pending(db, reconcile_settings)

    assert report.reopened == 1
    rows = await loans_of(db)
    assert len(rows) == 1
    assert rows[0]["state"] == "open"


async def test_a_genuine_reborrow_after_the_window_is_a_new_loan(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(db, [], at=T0 + DAY, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-06-01")],
        at=T0 + 30 * DAY,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    rows = await loans_of(db)
    assert len(rows) == 2
    assert {row["state"] for row in rows} == {"open", "returned"}
    # Both borrows are of the same work.
    assert len({row["media_id"] for row in rows}) == 1


async def test_two_copies_of_one_title_are_two_loans(db: Database, reconcile_settings: Settings, account: int) -> None:
    """A household can have two copies of the same game out at once."""
    both = [
        loan("Catan", item_id="7", due="2026-03-20", media_type="Konventionelles Spiel", author=None),
        loan("Catan", item_id="7", due="2026-03-29", media_type="Konventionelles Spiel", author=None),
    ]
    await record_poll(db, both, at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    rows = await loans_of(db)
    assert len(rows) == 2

    await record_poll(db, [both[1]], at=T0 + 2 * DAY, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    rows = await loans_of(db)
    states = sorted(row["state"] for row in rows)
    assert states == ["open", "returned"]


async def test_a_failed_poll_never_closes_a_loan(db: Database, reconcile_settings: Settings, account: int) -> None:
    """The whole point of classifying failures."""
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    run_id = await queries.start_run(db, account, "schedule", T0 + DAY)
    await queries.finish_run(
        db,
        run_id,
        status="auth_error",
        finished_at=T0 + DAY,
        duration_ms=5,
        error_kind="AuthenticationError",
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)

    (row,) = await loans_of(db)
    assert row["state"] == "open"


async def test_a_suspect_run_is_not_reconciled(db: Database, reconcile_settings: Settings, account: int) -> None:
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    run_id = await queries.start_run(db, account, "schedule", T0 + DAY)
    await queries.finish_run(
        db, run_id, status="suspect", finished_at=T0 + DAY, duration_ms=5, loan_count=0, account_id=account
    )
    await reconcile_pending(db, reconcile_settings)

    (row,) = await loans_of(db)
    assert row["state"] == "open"


async def test_rebuild_reproduces_the_same_history(db: Database, reconcile_settings: Settings, account: int) -> None:
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-03-29"), loan("Tschick", item_id="2", due="2026-03-29")],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)
    await record_poll(
        db, [loan("Momo", item_id="1", due="2026-04-26", times_renewed=1)], at=T0 + 4 * DAY, account_id=account
    )
    await reconcile_pending(db, reconcile_settings)

    before = [
        {k: v for k, v in row.items() if k not in {"id", "copy_id", "media_id", "derived_at"}}
        for row in await loans_of(db)
    ]

    await rebuild_history(db, reconcile_settings)

    after = [
        {k: v for k, v in row.items() if k not in {"id", "copy_id", "media_id", "derived_at"}}
        for row in await loans_of(db)
    ]
    assert after == before


async def test_a_manual_correction_survives_a_rebuild(db: Database, reconcile_settings: Settings, account: int) -> None:
    """Corrections live outside the derived layer precisely so that
    recomputing history does not throw them away."""
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    (row,) = await loans_of(db)
    async with db.write() as w:
        await w.execute(
            "INSERT INTO loan_overrides (loan_key, lend_date, note) VALUES (?, ?, ?)",
            (row["loan_key"], "2026-02-14", "receipt from the counter"),
        )

    await rebuild_history(db, reconcile_settings)

    (rebuilt,) = await loans_of(db)
    assert rebuilt["lend_date"] == "2026-02-14"
    assert rebuilt["lend_date_source"] == "manual"
    assert rebuilt["duration_uncertainty_days"] == 0


async def test_repeat_borrows_share_one_work(db: Database, reconcile_settings: Settings, account: int) -> None:
    for offset, due in ((0, "2026-03-29"), (40, "2026-05-29")):
        await record_poll(
            db,
            [loan("Momo", item_id="1", due=due)] if offset == 0 else [],
            at=T0 + offset * DAY,
            account_id=account,
        )
        await reconcile_pending(db, reconcile_settings)

    media = await db.fetch_all("SELECT COUNT(*) AS n FROM media")
    assert media[0]["n"] == 1


async def test_duration_and_uncertainty_are_available_for_statistics(
    db: Database, reconcile_settings: Settings, account: int
) -> None:
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-03-29", times_renewed=2)],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, reconcile_settings)
    await record_poll(db, [], at=T0 + 10 * DAY, account_id=account)
    await reconcile_pending(db, reconcile_settings)

    rows = await db.fetch_all("SELECT * FROM v_loan_durations")
    assert len(rows) == 1
    assert rows[0]["days_held"] == 0
    assert rows[0]["lend_unreliable"] == 1
    assert date.fromisoformat(rows[0]["eff_return_date"]) == T0.date()
