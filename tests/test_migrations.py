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
        assert version == latest_version() == 4
        assert current_version(conn) == 4
        assert {
            "accounts",
            "poll_runs",
            "snapshot_items",
            "loans",
            "media",
            "lookup_probes",
            "detail_lookups",
        } <= _tables(conn)
        # 0004: the rebuilt poll_runs accepts the rate-limit status and still
        # rejects a bogus one
        conn.execute("INSERT INTO accounts (name, library_type, username) VALUES ('a', 'remseck', 'u')")
        conn.execute(
            "INSERT INTO poll_runs (account_id, trigger, status, started_at)"
            " VALUES (1, 'manual', 'rate_limited', '2026-01-01T00:00:00+00:00')"
        )
        try:
            conn.execute(
                "INSERT INTO poll_runs (account_id, trigger, status, started_at)"
                " VALUES (1, 'manual', 'bogus', '2026-01-01T00:00:00+00:00')"
            )
            raise AssertionError("CHECK constraint did not reject a bogus status")
        except sqlite3.IntegrityError:
            pass
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


def test_rebuilding_a_parent_table_keeps_its_children(db_path: Path) -> None:
    """0004 drops and recreates poll_runs while `fees` still REFERENCES it.

    With foreign-key enforcement on — which is how the service opens its
    connection — SQLite refuses to drop a parent that has referencing rows.
    This is the live failure the migration first hit on omo: the unit test
    above passed only because its tables were empty. Reproduce it with a
    version-3 database that holds real observations.
    """
    conn = connect(db_path)
    try:
        for target, _name, sql in migrator.discover():
            if target > 3:
                continue
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;")
        conn.execute("INSERT INTO accounts (name, library_type, username) VALUES ('a', 'remseck', 'u')")
        conn.execute(
            "INSERT INTO poll_runs (id, account_id, trigger, status, started_at)"
            " VALUES (1, 1, 'manual', 'success', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO fees (account_id, fee_key, description, amount_cents, first_seen_run_id, last_seen_run_id)"
            " VALUES (1, 'f', 'late fee', 150, 1, 1)"
        )

        assert migrate(conn) == 4

        # The child kept its foreign key — and it is enforced afterwards.
        assert any(row["table"] == "poll_runs" for row in conn.execute("PRAGMA foreign_key_list(fees)"))
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        try:
            conn.execute(
                "INSERT INTO fees (account_id, fee_key, description, amount_cents, first_seen_run_id, last_seen_run_id)"
                " VALUES (1, 'g', 'ghost', 1, 999, 1)"
            )
            raise AssertionError("foreign keys stopped being enforced after the migration")
        except sqlite3.IntegrityError:
            pass
        # The observations survived the rebuild.
        assert conn.execute("SELECT status FROM poll_runs WHERE id = 1").fetchone()["status"] == "success"
    finally:
        conn.close()


def test_a_migration_is_rejected_when_it_breaks_a_foreign_key(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FK enforcement is off while statements run; the foreign_key_check at
    the end of each script is what still catches broken references."""
    conn = connect(db_path)
    try:
        conn.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY); CREATE TABLE kid (p INTEGER REFERENCES parent(id));"
        )
        broken = [(1, "0001_orphan.sql", "INSERT INTO kid VALUES (42);")]
        monkeypatch.setattr(migrator, "discover", lambda: broken)
        with pytest.raises(RuntimeError, match="0001_orphan\\.sql failed"):
            migrate(conn)
        assert current_version(conn) == 0
    finally:
        conn.close()


def test_duplicate_versions_are_rejected() -> None:
    dupes = [(1, "0001_a.sql", ""), (1, "0001_b.sql", "")]
    with pytest.raises(RuntimeError, match="Duplicate migration version 1"):
        migrator._reject_duplicates(dupes)
