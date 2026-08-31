"""Migration runner.

Numbered plain-SQL files, one transaction each, version tracked in
``PRAGMA user_version`` so the bump is atomic with the statements it belongs
to. No alembic: it drags in SQLAlchemy and its autogenerate is useless without
ORM models, while these files stay readable inside the nix store.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

MIGRATIONS_PACKAGE = "bib_tracker.db.migrations"
_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer version of bib-tracker.

    Refusing to start is the right response after a NixOS rollback: an older
    binary against a newer schema would silently misread or corrupt data.
    """


def discover() -> list[tuple[int, str, str]]:
    """Return ``(version, name, sql)`` for every migration, in order."""
    found: list[tuple[int, str, str]] = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        match = _FILENAME.match(entry.name)
        if match is None:
            continue
        found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    _reject_duplicates(found)
    return found


def _reject_duplicates(found: list[tuple[int, str, str]]) -> None:
    seen: dict[int, str] = {}
    for version, name, _ in found:
        if version in seen:
            raise RuntimeError(f"Duplicate migration version {version}: {seen[version]} and {name}")
        seen[version] = name


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def latest_version() -> int:
    migrations = discover()
    return migrations[-1][0] if migrations else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration. Returns the resulting schema version."""
    migrations = discover()
    version = current_version(conn)
    newest = migrations[-1][0] if migrations else 0
    if version > newest:
        raise SchemaTooNewError(f"Database schema is at version {version}, but this build only knows up to {newest}")

    for target, name, sql in migrations:
        if target <= version:
            continue
        # BEGIN/COMMIT go inside the script: executescript() would otherwise
        # commit an already-open transaction before running a single statement,
        # leaving a half-applied migration behind on failure. Migration files
        # therefore must not open transactions of their own.
        script = f"BEGIN;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception as err:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise RuntimeError(f"Migration {name} failed: {err}") from err
        version = target

    return version


def migrate_path(path: Path | str) -> int:
    from .connection import connect

    conn = connect(path)
    try:
        return migrate(conn)
    finally:
        conn.close()
