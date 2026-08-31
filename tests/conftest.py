"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bib_tracker.db.connection import Database, connect
from bib_tracker.db.migrator import migrate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "bib-tracker.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    conn = connect(db_path)
    migrate(conn)
    conn.close()

    database = Database(db_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def settings(db_path: Path):
    from bib_tracker.config import Settings

    return Settings(db_path=db_path, metadata_enabled=False)


@pytest.fixture
async def client(settings):
    """ASGI client against a fully wired app (lifespan runs the migrations)."""
    import httpx
    from asgi_lifespan import LifespanManager  # type: ignore[import-not-found]

    from bib_tracker.app import create_app

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
