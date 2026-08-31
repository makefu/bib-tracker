"""Console entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import load_settings


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO), format="%(levelname)s %(name)s: %(message)s"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the web server."""
    parser = argparse.ArgumentParser(prog="bib-tracker", description="bib-tracker web server")
    parser.add_argument("--version", action="version", version=f"bib-tracker {__version__}")
    parser.parse_args(argv)

    import uvicorn

    from .app import create_app

    settings = load_settings()
    _configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level)
    return 0


def migrate(argv: list[str] | None = None) -> int:
    """Apply pending migrations, then exit.

    Runs as ExecStartPre so a schema problem stops the unit before it ever
    binds a port.
    """
    parser = argparse.ArgumentParser(prog="bib-tracker-migrate", description="Apply database migrations")
    parser.add_argument("--version", action="version", version=f"bib-tracker {__version__}")
    parser.parse_args(argv)

    from .db.migrator import migrate_path

    settings = load_settings()
    _configure_logging(settings.log_level)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    version = migrate_path(settings.db_path)
    print(f"schema version {version} at {settings.db_path}")
    return 0


def poll_once(argv: list[str] | None = None) -> int:
    """Poll every enabled account once and exit non-zero on failure."""
    parser = argparse.ArgumentParser(prog="bib-tracker-poll", description="Poll every account once")
    parser.add_argument("--account", help="Poll only this account")
    parser.add_argument("--version", action="version", version=f"bib-tracker {__version__}")
    args = parser.parse_args(argv)

    return asyncio.run(_poll_once(args.account))


async def _poll_once(only: str | None) -> int:
    from .db import queries
    from .db.connection import Database
    from .db.migrator import migrate_path
    from .services import PollService

    settings = load_settings()
    _configure_logging(settings.log_level)
    migrate_path(settings.db_path)

    db = Database(settings.db_path)
    accounts = settings.load_accounts()
    if only is not None:
        accounts = [a for a in accounts if a.name == only]
        if not accounts:
            print(f"No such account: {only}", file=sys.stderr)
            return 2

    await queries.sync_accounts(db, accounts)
    service = PollService(db, settings, accounts)
    try:
        runs = await service.poll_all(trigger="manual")
        failed = 0
        for name, run_id in runs.items():
            if run_id is None:
                print(f"{name}: could not be polled", file=sys.stderr)
                failed += 1
                continue
            run = await queries.get_run(db, run_id)
            status = run["status"] if run else "unknown"
            print(f"{name}: {status} ({run['loan_count'] if run else '?'} loans)")
            if status not in {"success", "suspect"}:
                failed += 1
        return 1 if failed else 0
    finally:
        await service.aclose()
        db.close()


def rebuild(argv: list[str] | None = None) -> int:
    """Recompute the derived lending history from the stored observations."""
    parser = argparse.ArgumentParser(
        prog="bib-tracker-rebuild",
        description="Recompute the lending history from the recorded observations",
    )
    parser.add_argument("--version", action="version", version=f"bib-tracker {__version__}")
    parser.parse_args(argv)

    return asyncio.run(_rebuild())


async def _rebuild() -> int:
    from .db.connection import Database
    from .library.reconcile import rebuild_history

    settings = load_settings()
    _configure_logging(settings.log_level)

    db = Database(settings.db_path)
    try:
        accounts = {a.name: a for a in settings.load_accounts()}
        report = await rebuild_history(db, settings, accounts)
        print(
            f"rebuilt: {report.opened} opened, {report.closed} closed, "
            f"{report.reopened} reopened, {report.renewed} renewals"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
