"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, load_settings
from .db.connection import Database, connect
from .db.migrator import current_version, migrate
from .web import STATIC_DIR

_LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # bib-tracker-migrate already ran as ExecStartPre; doing it again here
        # keeps a bare `python -m bib_tracker` usable and is a no-op otherwise.
        conn = connect(settings.db_path)
        try:
            migrate(conn)
        finally:
            conn.close()

        app.state.db = Database(settings.db_path)
        app.state.settings = settings
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(
        title="bib-tracker",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        db: Database = app.state.db
        schema_version = await _schema_version(db)
        accounts = await db.fetch_all("SELECT name, last_success_at FROM accounts WHERE removed_at IS NULL")
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "schema_version": schema_version,
                "accounts": [{"name": row["name"], "last_success_at": row["last_success_at"]} for row in accounts],
            }
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


async def _schema_version(db: Database) -> int:
    import asyncio

    return await asyncio.to_thread(lambda: current_version(db.connection))
