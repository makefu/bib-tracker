"""Database access layer."""

from __future__ import annotations

import asyncio

import pytest

from bib_tracker.db.connection import Database


async def test_write_transaction_commits(db: Database) -> None:
    async with db.write() as w:
        await w.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("theme", "dark"))

    row = await db.fetch_one("SELECT value FROM settings WHERE key = ?", ("theme",))
    assert row is not None
    assert row["value"] == "dark"


async def test_write_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(ValueError):
        async with db.write() as w:
            await w.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("theme", "dark"))
            raise ValueError("boom")

    assert await db.fetch_all("SELECT * FROM settings") == []


async def test_foreign_keys_are_enforced(db: Database) -> None:
    """Without this a deleted account would leave orphaned history behind."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as w:
            await w.execute(
                "INSERT INTO poll_runs (account_id, trigger, status, started_at)"
                " VALUES (999, 'manual', 'running', datetime('now'))"
            )


async def test_concurrent_writers_are_serialised(db: Database) -> None:
    """SQLite allows one writer; queueing beats surfacing SQLITE_BUSY."""

    async def insert(n: int) -> None:
        async with db.write() as w:
            await w.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (f"k{n}", str(n)))

    await asyncio.gather(*(insert(n) for n in range(20)))
    rows = await db.fetch_all("SELECT key FROM settings")
    assert len(rows) == 20
