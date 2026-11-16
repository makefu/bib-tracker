"""Named SQL operations returning typed rows.

Raw SQL on purpose: every interesting query here is analytic (durations, money
saved, per-class histograms) and an ORM would only obscure it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .connection import Database, Writer


@dataclass(frozen=True)
class AccountRow:
    id: int
    name: str
    library_type: str
    username: str
    base_url: str | None
    enabled: bool


#: How the account's site last treated us. Drives the poll back-off.
@dataclass(frozen=True)
class BackoffState:
    until: datetime | None
    failures: int


def _aware(value: str) -> datetime:
    """Parse a stored timestamp; SQL's datetime() writes naive UTC strings."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def get_backoff(db: Database, account_id: int) -> BackoffState:
    row = await db.fetch_one(
        "SELECT backoff_until, backoff_failures FROM accounts WHERE id = ?",
        (account_id,),
    )
    until = row["backoff_until"] if row else None
    return BackoffState(
        until=_aware(until) if until else None,
        failures=int(row["backoff_failures"]) if row else 0,
    )


async def bump_backoff(
    db: Database,
    account_id: int,
    *,
    base_minutes: int,
    max_hours: int,
    retry_after: float | None = None,
    now: datetime | None = None,
) -> datetime:
    """Record another failure and return the time the account may be tried again.

    Exponential on the consecutive-failure count, capped, and never shorter
    than a ``Retry-After`` the server itself asked for.
    """
    start = now or datetime.now(UTC)
    row = await db.fetch_one("SELECT backoff_failures FROM accounts WHERE id = ?", (account_id,))
    failures = int(row["backoff_failures"]) + 1 if row else 1
    delay_minutes = min(base_minutes * 2 ** (failures - 1), max_hours * 60)
    if retry_after is not None:
        delay_minutes = max(delay_minutes, retry_after / 60)
    until = start + timedelta(minutes=delay_minutes)
    async with db.write() as w:
        await w.execute(
            "UPDATE accounts SET backoff_failures = ?, backoff_until = ? WHERE id = ?",
            (failures, until.isoformat(), account_id),
        )
    return until


async def clear_backoff(db: Database, account_id: int) -> None:
    async with db.write() as w:
        await w.execute(
            "UPDATE accounts SET backoff_failures = 0, backoff_until = NULL WHERE id = ?",
            (account_id,),
        )


async def get_detail_lookups(db: Database, account_id: int, *, max_age_days: int) -> dict[str, dict[str, Any]]:
    """copy_key -> what the detail page said, for gating per-loan fetches.

    Rows older than ``max_age`` are omitted so a copy is re-fetched after it.
    """
    rows = await db.fetch_all(
        "SELECT copy_key, cover_url, isbn FROM detail_lookups"
        " WHERE account_id = ? AND fetched_at >= datetime('now', ?)",
        (account_id, f"-{max_age_days} days"),
    )
    return {row["copy_key"]: dict(row) for row in rows}


async def store_detail_lookup(
    db: Database,
    account_id: int,
    copy_key: str,
    *,
    cover_url: str | None,
    isbn: str | None,
) -> None:
    """Record what the detail page said — including "nothing", which is what
    stops a title the catalogue has no jacket for from costing a request on
    every poll."""
    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO detail_lookups (account_id, copy_key, cover_url, isbn, fetched_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT (account_id, copy_key) DO UPDATE SET
                cover_url  = excluded.cover_url,
                isbn       = excluded.isbn,
                fetched_at = excluded.fetched_at
            """,
            (account_id, copy_key, cover_url, isbn),
        )


async def account_id_by_name(db: Database, name: str) -> int | None:
    row = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
    return None if row is None else int(row["id"])


async def sync_accounts(db: Database, accounts: list[Any]) -> None:
    """Bring the accounts table in line with the declared configuration.

    Accounts that disappear from the config are marked removed rather than
    deleted: their history is still worth keeping.
    """
    declared = {account.name for account in accounts}
    async with db.write() as w:
        for account in accounts:
            await w.execute(
                """
                INSERT INTO accounts (name, library_type, username, base_url,
                                      display_name, colour, enabled, removed_at)
                VALUES (:name, :library_type, :username, :base_url,
                        :display_name, :colour, :enabled, NULL)
                ON CONFLICT (name) DO UPDATE SET
                    library_type = excluded.library_type,
                    username     = excluded.username,
                    base_url     = excluded.base_url,
                    display_name = excluded.display_name,
                    colour       = excluded.colour,
                    enabled      = excluded.enabled,
                    removed_at   = NULL
                """,
                {
                    "name": account.name,
                    "library_type": account.library_type,
                    "username": account.username,
                    "base_url": account.base_url,
                    "display_name": account.display_name,
                    "colour": account.colour,
                    "enabled": int(account.enabled),
                },
            )

        rows = await w.fetch_all("SELECT name FROM accounts WHERE removed_at IS NULL")
        for row in rows:
            if row["name"] not in declared:
                await w.execute(
                    "UPDATE accounts SET removed_at = datetime('now'), enabled = 0 WHERE name = ?",
                    (row["name"],),
                )


async def get_account(db: Database, name: str) -> AccountRow | None:
    row = await db.fetch_one(
        "SELECT id, name, library_type, username, base_url, enabled FROM accounts WHERE name = ?",
        (name,),
    )
    if row is None:
        return None
    return AccountRow(
        id=row["id"],
        name=row["name"],
        library_type=row["library_type"],
        username=row["username"],
        base_url=row["base_url"],
        enabled=bool(row["enabled"]),
    )


async def start_run(db: Database, account_id: int, trigger: str, started_at: datetime) -> int:
    async with db.write() as w:
        return await w.execute(
            "INSERT INTO poll_runs (account_id, trigger, status, started_at) VALUES (?, ?, 'running', ?)",
            (account_id, trigger, started_at.isoformat()),
        )


async def open_loan_count(db: Database, account_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM loans WHERE account_id = ? AND state = 'open'",
        (account_id,),
    )
    return int(row["n"]) if row else 0


async def last_successful_loan_count(db: Database, account_id: int) -> int | None:
    """Loan count of the newest reconciled successful run, if there is one."""
    row = await db.fetch_one(
        """
        SELECT loan_count FROM poll_runs
        WHERE account_id = ? AND status = 'success' AND reconciled = 1
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (account_id,),
    )
    return None if row is None else int(row["loan_count"] or 0)


async def consecutive_suspect_runs(db: Database, account_id: int) -> int:
    """How many suspect runs sit unbroken at the head of this account's log."""
    rows = await db.fetch_all(
        "SELECT status FROM poll_runs WHERE account_id = ? AND status IN ('success', 'suspect')"
        " ORDER BY started_at DESC, id DESC LIMIT 20",
        (account_id,),
    )
    count = 0
    for row in rows:
        if row["status"] != "suspect":
            break
        count += 1
    return count


async def finish_run(
    db: Database,
    run_id: int,
    *,
    status: str,
    finished_at: datetime,
    duration_ms: int,
    loan_count: int | None = None,
    fee_count: int | None = None,
    fees_supported: bool = True,
    suspect_reason: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
    snapshot: list[dict[str, Any]] | None = None,
    account_id: int | None = None,
    observed_at: datetime | None = None,
) -> None:
    """Close out a run and, for a usable one, store its observations.

    Snapshot rows are written only for runs whose result can be trusted, so a
    failed poll leaves no trace that a later rebuild could misread as truth.
    """
    async with db.write() as w:
        await w.execute(
            """
            UPDATE poll_runs SET
                status = :status, finished_at = :finished_at, duration_ms = :duration_ms,
                loan_count = :loan_count, fee_count = :fee_count, fees_supported = :fees_supported,
                suspect_reason = :suspect_reason, error_kind = :error_kind, error_message = :error_message
            WHERE id = :run_id
            """,
            {
                "status": status,
                "finished_at": finished_at.isoformat(),
                "duration_ms": duration_ms,
                "loan_count": loan_count,
                "fee_count": fee_count,
                "fees_supported": int(fees_supported),
                "suspect_reason": suspect_reason,
                "error_kind": error_kind,
                "error_message": error_message,
                "run_id": run_id,
            },
        )

        if snapshot and account_id is not None:
            await _insert_snapshot(w, run_id, account_id, observed_at or finished_at, snapshot)

        if status == "success" and account_id is not None:
            await w.execute(
                "UPDATE accounts SET last_success_at = ? WHERE id = ?",
                (finished_at.isoformat(), account_id),
            )


async def _insert_snapshot(
    w: Writer,
    run_id: int,
    account_id: int,
    observed_at: datetime,
    items: list[dict[str, Any]],
) -> None:
    await w.execute_many(
        """
        INSERT INTO snapshot_items (
            run_id, account_id, observed_at, copy_key, raw_json, title, author, publisher,
            media_type, item_id, barcode, call_number, branch, due_date, checkout_date,
            times_renewed, max_renewals, can_be_renewed, isbn, cover_url, detail_url
        ) VALUES (
            :run_id, :account_id, :observed_at, :copy_key, :raw_json, :title, :author, :publisher,
            :media_type, :item_id, :barcode, :call_number, :branch, :due_date, :checkout_date,
            :times_renewed, :max_renewals, :can_be_renewed, :isbn, :cover_url, :detail_url
        )
        """,
        [
            {
                "run_id": run_id,
                "account_id": account_id,
                "observed_at": observed_at.isoformat(),
                "copy_key": item["copy_key"],
                "raw_json": json.dumps(item["raw"], ensure_ascii=False, sort_keys=True),
                "title": item["raw"]["title"],
                "author": item["raw"].get("author"),
                "publisher": item["raw"].get("publisher"),
                "media_type": item["raw"].get("media_type"),
                "item_id": item["raw"].get("item_id"),
                "barcode": item["raw"].get("barcode"),
                "call_number": item["raw"].get("call_number"),
                "branch": item["raw"].get("library_branch"),
                "due_date": item["raw"]["due_date"],
                "checkout_date": item["raw"].get("checkout_date"),
                "times_renewed": item["raw"].get("times_renewed") or 0,
                "max_renewals": item["raw"].get("max_renewals"),
                "can_be_renewed": int(bool(item["raw"].get("can_be_renewed"))),
                "isbn": item["raw"].get("isbn"),
                "cover_url": item["raw"].get("cover_url"),
                "detail_url": item["raw"].get("detail_url"),
            }
            for item in items
        ],
    )


async def latest_snapshot(db: Database, account_id: int) -> list[dict[str, Any]]:
    """The items seen by the newest usable run, for the current-loans view."""
    row = await db.fetch_one(
        "SELECT id FROM poll_runs WHERE account_id = ? AND status IN ('success', 'suspect')"
        " AND finished_at IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1",
        (account_id,),
    )
    if row is None:
        return []
    rows = await db.fetch_all(
        "SELECT * FROM snapshot_items WHERE run_id = ? ORDER BY due_date, title",
        (row["id"],),
    )
    return [dict(item) for item in rows]


async def get_run(db: Database, run_id: int) -> dict[str, Any] | None:
    row = await db.fetch_one("SELECT * FROM poll_runs WHERE id = ?", (run_id,))
    return dict(row) if row else None


async def latest_run(db: Database, account_id: int | None = None) -> dict[str, Any] | None:
    if account_id is None:
        row = await db.fetch_one("SELECT * FROM poll_runs ORDER BY started_at DESC, id DESC LIMIT 1")
    else:
        row = await db.fetch_one(
            "SELECT * FROM poll_runs WHERE account_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
            (account_id,),
        )
    return dict(row) if row else None


async def promote_suspect_runs(db: Database, account_id: int) -> int:
    """Accept the suspect runs at the head of this account's log.

    Called once a later poll confirms what they saw. They are promoted rather
    than discarded so the history keeps the earlier date, which is when the
    change actually happened.
    """
    rows = await db.fetch_all(
        "SELECT id, status FROM poll_runs WHERE account_id = ? AND status IN ('success', 'suspect')"
        " ORDER BY started_at DESC, id DESC LIMIT 20",
        (account_id,),
    )
    pending = []
    for row in rows:
        if row["status"] != "suspect":
            break
        pending.append(row["id"])

    if not pending:
        return 0

    async with db.write() as w:
        await w.execute(
            f"UPDATE poll_runs SET status = 'success' WHERE id IN ({','.join('?' * len(pending))})",
            pending,
        )
    return len(pending)
