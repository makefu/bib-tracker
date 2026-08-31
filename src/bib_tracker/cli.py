"""Console entry points."""

from __future__ import annotations

import argparse
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
    raise SystemExit("not implemented yet")


def rebuild(argv: list[str] | None = None) -> int:
    """Recompute the derived lending history from the stored observations."""
    raise SystemExit("not implemented yet")


if __name__ == "__main__":
    sys.exit(main())
