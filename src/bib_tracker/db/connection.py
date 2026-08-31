"""SQLite access.

One connection per thread, async calls hopping through ``asyncio.to_thread``.
Not aiosqlite -- that is a thread wrapper too, and the stdlib version is also
usable from the synchronous migration runner and from the one-shot CLIs, which
have no event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

Params = Sequence[Any] | dict[str, Any]

#: WAL gives concurrent readers; a single writer is all SQLite offers either
#: way, so writes are serialised explicitly rather than left to SQLITE_BUSY.
_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a configured connection. Used by the async layer and the CLIs."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


class Database:
    """Thread-affine connection pool with an async facade."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    # -- synchronous API, for the migration runner and tests ---------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def fetch_all_sync(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params))

    def fetch_one_sync(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.connection.execute(sql, params).fetchone()
        return row

    # -- async API ---------------------------------------------------------

    async def fetch_all(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self.fetch_all_sync, sql, params)

    async def fetch_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(self.fetch_one_sync, sql, params)

    @asynccontextmanager
    async def write(self) -> AsyncIterator[Writer]:
        """Serialise writers and run their statements off the event loop."""
        async with self._write_lock:
            writer = Writer(self)
            await asyncio.to_thread(writer._begin)
            try:
                yield writer
            except BaseException:
                await asyncio.to_thread(writer._rollback)
                raise
            await asyncio.to_thread(writer._commit)

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                conn.close()
            self._connections.clear()
        self._local = threading.local()


class Writer:
    """Statements inside one write transaction, executed in a worker thread."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _begin(self) -> None:
        self._conn = self._db.connection
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Run a statement; returns lastrowid."""

        def run() -> int:
            cur = self._conn.execute(sql, params)
            return int(cur.lastrowid or 0)

        return await asyncio.to_thread(run)

    async def execute_many(self, sql: str, rows: Sequence[Params]) -> None:
        await asyncio.to_thread(lambda: self._conn.executemany(sql, rows))

    async def fetch_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(lambda: self._conn.execute(sql, params).fetchone())

    async def fetch_all(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(lambda: list(self._conn.execute(sql, params)))
