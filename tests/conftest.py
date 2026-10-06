"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from bib_tracker.db.connection import Database, connect
from bib_tracker.db.migrator import migrate

FIXTURES = Path(__file__).parent / "fixtures"

# The browser tests import playwright at module level. Where the e2e
# dependencies were not added to the environment, the whole directory is
# ignored at collection rather than failing the run at import.
try:
    import pytest_playwright  # noqa: F401
except ImportError:  # pragma: no cover
    collect_ignore_glob = ["e2e/*"]


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
def settings(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bib_tracker.config import SETTINGS_ENV_PREFIX, Settings

    # A BIB_TRACKER_* var exported in the developer's shell must not leak
    # into a test.
    for key in list(os.environ):
        if key.startswith(SETTINGS_ENV_PREFIX):
            monkeypatch.delenv(key)

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


@pytest.fixture
def account_config(tmp_path: Path):
    """A Remseck account pointed at a respx-mocked host."""
    from bib_tracker.config import AccountConfig

    password_file = tmp_path / "password"
    password_file.write_text("hunter2\n")
    return AccountConfig(
        name="remseck",
        library_type="remseck",
        username="12345",
        base_url="http://opac.test",
        password_file=password_file,
    )


@pytest.fixture
async def poll_service(db, settings, account_config):
    from bib_tracker.db import queries
    from bib_tracker.services import PollService

    await queries.sync_accounts(db, [account_config])
    service = PollService(db, settings, [account_config])
    try:
        yield service
    finally:
        await service.aclose()


def library_fixture(name: str) -> str:
    return (FIXTURES / "library" / name).read_text(encoding="utf-8")


def recorded_fixture(name: str) -> str:
    """A page as one of the live OPACs actually served it.

    Recorded and anonymised in ha_stadtbibliothek and copied here; refresh
    both with `nix run .#record-fixtures` in that repository.
    """
    return (FIXTURES / "library" / "recorded" / name).read_text(encoding="utf-8")


@pytest.fixture
async def api(settings, account_config, tmp_path):
    """An app with one account, pointed at a respx-mocked OPAC."""
    import httpx
    from asgi_lifespan import LifespanManager

    from bib_tracker.app import create_app

    settings.accounts = [account_config]
    settings.poll_on_startup = False
    settings.metadata_enabled = True

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
