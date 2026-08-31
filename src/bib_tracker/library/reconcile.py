"""Turn observations into lending history.

The OPACs only ever say what is on loan right now, so a loan's start and end
have to be inferred from when its copy appeared in and disappeared from the
snapshots. Every inferred date is stored with a source and a pair of bounds,
so the interface and the statistics can be honest about what is actually known.

Everything here is derived: rebuild_history() drops the lot and recomputes it
from the observation layer, which is what makes a reconciler bug found later
fixable for history already recorded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..config import AccountConfig, Settings
from ..db.connection import Database, Writer
from .identity import author_key, media_key
from .media_class import MediaClass, classify

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DateEstimate:
    """A date, how we arrived at it, and how wrong it might be."""

    value: date
    source: str
    earliest: date | None
    latest: date | None


@dataclass
class ReconcileReport:
    opened: int = 0
    closed: int = 0
    reopened: int = 0
    renewed: int = 0
    updated: int = 0


async def reconcile_pending(
    db: Database,
    settings: Settings,
    accounts: dict[str, AccountConfig] | None = None,
) -> ReconcileReport:
    """Fold every unreconciled successful run into the history, oldest first."""
    report = ReconcileReport()
    rows = await db.fetch_all(
        "SELECT id FROM poll_runs WHERE status = 'success' AND reconciled = 0"
        " AND finished_at IS NOT NULL ORDER BY started_at, id"
    )
    for row in rows:
        merge(report, await reconcile_run(db, settings, row["id"], accounts))
    return report


def merge(target: ReconcileReport, other: ReconcileReport) -> None:
    target.opened += other.opened
    target.closed += other.closed
    target.reopened += other.reopened
    target.renewed += other.renewed
    target.updated += other.updated


async def reconcile_run(
    db: Database,
    settings: Settings,
    run_id: int,
    accounts: dict[str, AccountConfig] | None = None,
) -> ReconcileReport:
    """Fold one run's observations into the derived history."""
    report = ReconcileReport()

    run = await db.fetch_one("SELECT * FROM poll_runs WHERE id = ?", (run_id,))
    if run is None or run["status"] != "success":
        # Only a trusted run may change history. Anything else -- a failure, or
        # a result still awaiting confirmation -- is left alone.
        return report

    account_id = run["account_id"]
    observed_at = datetime.fromisoformat(run["finished_at"])
    previous_at = await _previous_run_time(db, account_id, run_id)

    items = await db.fetch_all("SELECT * FROM snapshot_items WHERE run_id = ? ORDER BY id", (run_id,))
    account = await db.fetch_one("SELECT name, library_type FROM accounts WHERE id = ?", (account_id,))
    config = (accounts or {}).get(account["name"]) if account else None

    by_key: dict[str, list[Any]] = {}
    for item in items:
        by_key.setdefault(item["copy_key"], []).append(item)

    async with db.write() as w:
        open_loans = await _open_loans_by_key(w, account_id)

        for copy_key, observations in by_key.items():
            existing = open_loans.pop(copy_key, [])
            await _reconcile_key(
                w,
                settings,
                run,
                account_id,
                copy_key,
                observations,
                existing,
                observed_at,
                previous_at,
                config,
                report,
            )

        # Whatever is still open but was not seen has gone back.
        for loans in open_loans.values():
            for loan in loans:
                await _close_loan(w, loan, run_id, observed_at, previous_at, settings, report)

        await w.execute("UPDATE poll_runs SET reconciled = 1 WHERE id = ?", (run_id,))
        await apply_overrides(w)

    return report


async def _previous_run_time(db: Database, account_id: int, run_id: int) -> datetime | None:
    """When we last had a trustworthy look at this account."""
    row = await db.fetch_one(
        """
        SELECT finished_at FROM poll_runs
        WHERE account_id = ? AND id != ? AND status = 'success' AND reconciled = 1
              AND finished_at IS NOT NULL
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (account_id, run_id),
    )
    return datetime.fromisoformat(row["finished_at"]) if row else None


async def _open_loans_by_key(w: Writer, account_id: int) -> dict[str, list[Any]]:
    rows = await w.fetch_all(
        """
        SELECT l.*, c.copy_key FROM loans l
        JOIN copies c ON c.id = l.copy_id
        WHERE l.account_id = ? AND l.state = 'open'
        """,
        (account_id,),
    )
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row["copy_key"], []).append(row)
    return grouped


async def _reconcile_key(
    w: Writer,
    settings: Settings,
    run: Any,
    account_id: int,
    copy_key: str,
    observations: list[Any],
    existing: list[Any],
    observed_at: datetime,
    previous_at: datetime | None,
    config: AccountConfig | None,
    report: ReconcileReport,
) -> None:
    """Match this poll's rows for one copy against the loans already open.

    Usually one of each. Two copies of the same game can be out at once,
    though, so pair them off by due date and open or close the difference.
    """
    observations = sorted(observations, key=lambda item: item["due_date"])
    existing = sorted(existing, key=lambda row: row["last_due_date"])

    opened_here = 0
    for index, item in enumerate(observations):
        if index < len(existing):
            await _update_loan(w, existing[index], item, run, observed_at, report)
        else:
            await _open_loan(
                w,
                settings,
                run,
                account_id,
                copy_key,
                item,
                observed_at,
                previous_at,
                config,
                report,
                opened_here,
            )
            opened_here += 1

    for surplus in existing[len(observations) :]:
        await _close_loan(w, surplus, run["id"], observed_at, previous_at, settings, report)


async def _open_loan(
    w: Writer,
    settings: Settings,
    run: Any,
    account_id: int,
    copy_key: str,
    item: Any,
    observed_at: datetime,
    previous_at: datetime | None,
    config: AccountConfig | None,
    report: ReconcileReport,
    ordinal: int = 0,
) -> None:
    media_id = await _upsert_media(w, item, observed_at)
    copy_id = await _upsert_copy(w, account_id, copy_key, media_id, item, observed_at)

    reopened = await _reopen_recently_closed(w, copy_id, item, run, observed_at, settings, report)
    if reopened:
        return

    estimate = _estimate_lend_date(item, observed_at, previous_at, config)
    due_date = date.fromisoformat(item["due_date"])
    # Two copies of the same title can be out at once and share a copy_key, so
    # the key needs an ordinal. Observations are ordered by due date, making it
    # deterministic across a rebuild.
    loan_key = f"{copy_key}@{run['id']}"
    if ordinal:
        loan_key = f"{loan_key}#{ordinal}"

    await w.execute(
        """
        INSERT INTO loans (
            loan_key, account_id, copy_id, media_id, state,
            lend_date, lend_date_source, lend_date_earliest, lend_date_latest,
            first_seen_run_id, last_seen_run_id, first_seen_at, last_seen_at,
            first_due_date, last_due_date, times_renewed, max_renewals, can_be_renewed,
            was_overdue, max_overdue_days, observation_count, derived_at
        ) VALUES (
            :loan_key, :account_id, :copy_id, :media_id, 'open',
            :lend_date, :lend_source, :lend_earliest, :lend_latest,
            :run_id, :run_id, :observed_at, :observed_at,
            :due_date, :due_date, :times_renewed, :max_renewals, :can_be_renewed,
            :was_overdue, :overdue_days, 1, :observed_at
        )
        """,
        {
            "loan_key": loan_key,
            "account_id": account_id,
            "copy_id": copy_id,
            "media_id": media_id,
            "lend_date": estimate.value.isoformat(),
            "lend_source": estimate.source,
            "lend_earliest": estimate.earliest.isoformat() if estimate.earliest else None,
            "lend_latest": estimate.latest.isoformat() if estimate.latest else None,
            "run_id": run["id"],
            "observed_at": observed_at.isoformat(),
            "due_date": item["due_date"],
            "times_renewed": item["times_renewed"],
            "max_renewals": item["max_renewals"],
            "can_be_renewed": item["can_be_renewed"],
            "was_overdue": int(due_date < observed_at.date()),
            "overdue_days": max(0, (observed_at.date() - due_date).days),
        },
    )
    report.opened += 1


async def _reopen_recently_closed(
    w: Writer,
    copy_id: int,
    item: Any,
    run: Any,
    observed_at: datetime,
    settings: Settings,
    report: ReconcileReport,
) -> bool:
    """Undo a close that a single missed poll caused.

    A copy that vanished and came straight back is far more likely to have
    been missed by one poll than to have been returned and borrowed again
    across the counter. Where it really was re-borrowed, the interface offers
    to split the loan, which writes an override.
    """
    cutoff = observed_at - timedelta(hours=settings.reopen_window_hours)
    row = await w.fetch_one(
        """
        SELECT * FROM loans
        WHERE copy_id = ? AND state = 'returned' AND last_seen_at >= ? AND last_due_date <= ?
        ORDER BY last_seen_at DESC LIMIT 1
        """,
        (copy_id, cutoff.isoformat(), item["due_date"]),
    )
    if row is None:
        return False

    await w.execute(
        """
        UPDATE loans SET
            state = 'open', return_date = NULL, return_date_source = NULL,
            return_date_earliest = NULL, return_date_latest = NULL, closing_run_id = NULL,
            last_seen_run_id = :run_id, last_seen_at = :observed_at,
            last_due_date = :due_date, times_renewed = :times_renewed,
            observation_count = observation_count + 1, derived_at = :observed_at
        WHERE id = :id
        """,
        {
            "run_id": run["id"],
            "observed_at": observed_at.isoformat(),
            "due_date": item["due_date"],
            "times_renewed": item["times_renewed"],
            "id": row["id"],
        },
    )
    report.reopened += 1
    return True


async def _update_loan(
    w: Writer,
    loan: Any,
    item: Any,
    run: Any,
    observed_at: datetime,
    report: ReconcileReport,
) -> None:
    due_date = date.fromisoformat(item["due_date"])
    overdue_days = max(0, (observed_at.date() - due_date).days)

    if item["times_renewed"] > loan["times_renewed"] or item["due_date"] > loan["last_due_date"]:
        await w.execute(
            """
            INSERT OR IGNORE INTO renewals (
                loan_key, observed_run_id, observed_at, due_date_before, due_date_after,
                times_renewed_before, times_renewed_after, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'observed')
            """,
            (
                loan["loan_key"],
                run["id"],
                observed_at.isoformat(),
                loan["last_due_date"],
                item["due_date"],
                loan["times_renewed"],
                item["times_renewed"],
            ),
        )
        report.renewed += 1

    await w.execute(
        """
        UPDATE loans SET
            last_seen_run_id = :run_id, last_seen_at = :observed_at,
            last_due_date = :due_date, times_renewed = :times_renewed,
            max_renewals = :max_renewals, can_be_renewed = :can_be_renewed,
            -- Latched, not recomputed: a renewal moves the due date, and
            -- recalculating later would erase that this was ever overdue.
            was_overdue = MAX(was_overdue, :now_overdue),
            max_overdue_days = MAX(max_overdue_days, :overdue_days),
            observation_count = observation_count + 1, derived_at = :observed_at
        WHERE id = :id
        """,
        {
            "run_id": run["id"],
            "observed_at": observed_at.isoformat(),
            "due_date": item["due_date"],
            "times_renewed": item["times_renewed"],
            "max_renewals": item["max_renewals"],
            "can_be_renewed": item["can_be_renewed"],
            "now_overdue": int(due_date < observed_at.date()),
            "overdue_days": overdue_days,
            "id": loan["id"],
        },
    )
    report.updated += 1


async def _close_loan(
    w: Writer,
    loan: Any,
    run_id: int,
    observed_at: datetime,
    previous_at: datetime | None,
    settings: Settings,
    report: ReconcileReport,
) -> None:
    """Record a return somewhere between the last sighting and now.

    The point estimate is the earlier bound on purpose: a long gap between
    polls would otherwise inflate every duration that spans it.
    """
    earliest = datetime.fromisoformat(loan["last_seen_at"]).date()
    latest = observed_at.date()
    if previous_at is not None:
        earliest = max(earliest, previous_at.date())

    await w.execute(
        """
        UPDATE loans SET
            state = 'returned', return_date = :return_date, return_date_source = 'last_seen',
            return_date_earliest = :earliest, return_date_latest = :latest,
            closing_run_id = :run_id, derived_at = :observed_at
        WHERE id = :id
        """,
        {
            "return_date": earliest.isoformat(),
            "earliest": earliest.isoformat(),
            "latest": latest.isoformat(),
            "run_id": run_id,
            "observed_at": observed_at.isoformat(),
            "id": loan["id"],
        },
    )
    report.closed += 1


def _estimate_lend_date(
    item: Any,
    observed_at: datetime,
    previous_at: datetime | None,
    config: AccountConfig | None,
) -> DateEstimate:
    """Work out when this was borrowed, and say how confident that is."""
    if item["checkout_date"]:
        exact = date.fromisoformat(item["checkout_date"])
        return DateEstimate(exact, "exact", exact, exact)

    seen = observed_at.date()
    window_start = previous_at.date() if previous_at else None

    # A never-renewed loan has a due date exactly one loan period after it
    # started. When that lands inside the window we did not observe, it is a
    # better answer than "whenever we happened to look".
    if not item["times_renewed"]:
        media_class = _media_class_of(item)
        period = config.loan_period(media_class.value) if config else _default_period(media_class)
        implied = date.fromisoformat(item["due_date"]) - timedelta(days=period)
        if implied <= seen and (window_start is None or implied >= window_start):
            return DateEstimate(implied, "due_minus_period", window_start or implied, seen)

    if window_start is None:
        # Already on loan when tracking began: the start is simply unknown.
        return DateEstimate(seen, "before_tracking", None, seen)

    return DateEstimate(seen, "first_seen", window_start, seen)


def _default_period(media_class: MediaClass) -> int:
    from ..config import DEFAULT_LOAN_PERIOD_DAYS

    return DEFAULT_LOAN_PERIOD_DAYS.get(media_class.value, 28)


def _media_class_of(item: Any) -> MediaClass:
    return classify(item["media_type"], call_number=item["call_number"], isbn=item["isbn"])


async def _upsert_media(w: Writer, item: Any, observed_at: datetime) -> int:
    media_class = _media_class_of(item)
    key = media_key(media_class, item["title"], item["author"])

    row = await w.fetch_one("SELECT id, raw_media_types FROM media WHERE media_key = ?", (key,))
    if row is not None:
        raw_types = set(json.loads(row["raw_media_types"]))
        if item["media_type"]:
            raw_types.add(item["media_type"])
        await w.execute(
            "UPDATE media SET last_seen_at = ?, raw_media_types = ?, updated_at = datetime('now') WHERE id = ?",
            (observed_at.isoformat(), json.dumps(sorted(raw_types)), row["id"]),
        )
        return int(row["id"])

    return await w.execute(
        """
        INSERT INTO media (
            media_key, media_class, title, author, author_key, raw_media_types,
            isbn13, publisher, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            media_class.value,
            item["title"],
            item["author"],
            author_key(item["author"]),
            json.dumps([item["media_type"]] if item["media_type"] else []),
            item["isbn"],
            item["publisher"],
            observed_at.isoformat(),
            observed_at.isoformat(),
        ),
    )


async def _upsert_copy(
    w: Writer,
    account_id: int,
    copy_key: str,
    media_id: int,
    item: Any,
    observed_at: datetime,
) -> int:
    row = await w.fetch_one(
        "SELECT id FROM copies WHERE account_id = ? AND copy_key = ?",
        (account_id, copy_key),
    )
    if row is not None:
        await w.execute("UPDATE copies SET last_seen_at = ? WHERE id = ?", (observed_at.isoformat(), row["id"]))
        return int(row["id"])

    account = await w.fetch_one("SELECT library_type FROM accounts WHERE id = ?", (account_id,))
    return await w.execute(
        """
        INSERT INTO copies (
            account_id, copy_key, media_id, library_type, item_id, barcode,
            call_number, branch, detail_url, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            copy_key,
            media_id,
            account["library_type"] if account else "",
            item["item_id"],
            item["barcode"],
            item["call_number"],
            item["branch"],
            item["detail_url"],
            observed_at.isoformat(),
            observed_at.isoformat(),
        ),
    )


async def apply_overrides(w: Writer) -> None:
    """Re-assert manual corrections over whatever the reconciler just decided."""
    rows = await w.fetch_all("SELECT * FROM loan_overrides")
    for row in rows:
        assignments = []
        params: dict[str, Any] = {"loan_key": row["loan_key"]}
        if row["lend_date"]:
            assignments.append("lend_date = :lend_date, lend_date_source = 'manual'")
            assignments.append("lend_date_earliest = :lend_date, lend_date_latest = :lend_date")
            params["lend_date"] = row["lend_date"]
        if row["return_date"]:
            assignments.append("return_date = :return_date, return_date_source = 'manual'")
            assignments.append("return_date_earliest = :return_date, return_date_latest = :return_date")
            params["return_date"] = row["return_date"]
        if row["state"]:
            assignments.append("state = :state")
            params["state"] = row["state"]
        if row["media_id"]:
            assignments.append("media_id = :media_id")
            params["media_id"] = row["media_id"]
        if assignments:
            await w.execute(
                f"UPDATE loans SET {', '.join(assignments)} WHERE loan_key = :loan_key",
                params,
            )


async def rebuild_history(
    db: Database,
    settings: Settings,
    accounts: dict[str, AccountConfig] | None = None,
) -> ReconcileReport:
    """Discard the derived history and recompute it from the observations.

    Safe by construction: nothing here is a source of truth. Manual overrides
    survive because loan_key is built from the opening run id, which is
    immutable observation-layer data.
    """
    async with db.write() as w:
        await w.execute("DELETE FROM renewals")
        await w.execute("DELETE FROM loans")
        await w.execute("DELETE FROM copies")
        await w.execute("UPDATE poll_runs SET reconciled = 0 WHERE status = 'success'")

    _LOGGER.info("Rebuilding lending history from stored observations")
    return await reconcile_pending(db, settings, accounts)
