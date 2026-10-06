"""Migration runner behaviour."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bib_tracker.db import migrator
from bib_tracker.db.connection import connect
from bib_tracker.db.migrator import SchemaTooNewError, current_version, latest_version, migrate


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {row["name"] for row in rows}


def test_migrate_creates_the_schema_and_records_the_version(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        assert current_version(conn) == 0
        version = migrate(conn)
        assert version == latest_version() == 2
        assert current_version(conn) == 2
        assert {"accounts", "poll_runs", "snapshot_items", "loans", "media", "lookup_probes"} <= _tables(conn)
    finally:
        conn.close()


def test_migrate_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        before = _tables(conn)
        assert migrate(conn) == latest_version()
        assert _tables(conn) == before
    finally:
        conn.close()


def test_migrate_refuses_a_newer_schema(db_path: Path) -> None:
    """After a NixOS rollback the old binary must refuse rather than misread."""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA user_version = 9999")
        with pytest.raises(SchemaTooNewError):
            migrate(conn)
    finally:
        conn.close()


def test_a_failing_migration_leaves_nothing_behind(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Atomicity matters: a half-applied schema is worse than none at all."""
    broken = [(1, "0001_broken.sql", "CREATE TABLE ok (id INTEGER); CREATE TABLE ok (id INTEGER);")]
    monkeypatch.setattr(migrator, "discover", lambda: broken)

    conn = connect(db_path)
    try:
        with pytest.raises(RuntimeError, match=r"0001_broken\.sql failed"):
            migrate(conn)
        assert current_version(conn) == 0
        assert "ok" not in _tables(conn)
    finally:
        conn.close()


def test_duplicate_versions_are_rejected() -> None:
    dupes = [(1, "0001_a.sql", ""), (1, "0001_b.sql", "")]
    with pytest.raises(RuntimeError, match="Duplicate migration version 1"):
        migrator._reject_duplicates(dupes)
