"""Operator-triggered maintenance: re-crawl, re-enrich, rebuild, compact.

Everything here exists to work on a database that grew before a feature
existed: old works have no price probes, no covers, no enrichment. The tasks
are idempotent and rate-limited by the same probe table the worker uses, so
starting one twice cannot double the requests to a shop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .config import Settings
from .db.connection import Database
from .metadata.worker import EnrichmentWorker

if TYPE_CHECKING:
    from .library.reconcile import ReconcileReport

_LOGGER = logging.getLogger(__name__)


@dataclass
class MaintenanceStatus:
    kind: str = ""
    label: str = ""
    total: int = 0
    done: int = 0
    started_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    detail: dict[str, int] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.finished_at is None and self.kind != ""


#: The kinds the API accepts, and their German labels.
KIND_LABELS: dict[str, str] = {
    "price": "Preis-Suche",
    "cover": "Cover-Suche",
    "reenrich": "Werkinformationen neu verschlagworten",
    "covers-reload": "Cover alle neu laden",
    "rebuild-history": "Verlauf neu aufbauen",
    "vacuum": "Datenbank komprimieren",
}


class MaintenanceBusy(RuntimeError):
    """Another maintenance task is already running."""


class MaintenanceRunner:
    """One maintenance task at a time; the progress feeds the settings card.

    A per-media re-crawl goes through the same runner (kinds price/cover with
    a one-element media list), so a button press and a global sweep can never
    hammer the same shop twice at once.
    """

    def __init__(self, db: Database, settings: Settings, worker: EnrichmentWorker | None = None) -> None:
        self._db = db
        self._settings = settings
        self._worker = worker
        self._task: asyncio.Task[None] | None = None
        self._status = MaintenanceStatus()

    def status(self) -> MaintenanceStatus:
        return self._status

    def start(
        self,
        kind: str,
        *,
        fresh: bool = False,
        scope: str = "all",
        media_ids: list[int] | None = None,
        clear_cache: bool = False,
    ) -> MaintenanceStatus:
        if self._task is not None and not self._task.done():
            raise MaintenanceBusy(self._status.kind)
        label = KIND_LABELS.get(kind, kind)
        self._status = MaintenanceStatus(kind=kind, label=label, started_at=datetime.now(UTC).isoformat())
        # An unawaited create_task keeps a strong reference until completion.
        self._task = asyncio.create_task(
            self._guard(self._dispatch(kind, fresh, scope, list(media_ids) if media_ids else None, clear_cache))
        )
        return self._status

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _guard(self, coro: object) -> None:
        """Never let a maintenance failure surface as an unhandled task error."""
        assert asyncio.iscoroutine(coro)
        try:
            await coro
        except asyncio.CancelledError:
            self._status.error = "abgebrochen"
            self._status.finished_at = datetime.now(UTC).isoformat()
            raise
        except Exception as err:  # the card shows whatever broke
            _LOGGER.warning("maintenance task failed", exc_info=True)
            self._status.error = str(err)
            self._status.finished_at = datetime.now(UTC).isoformat()

    async def _dispatch(
        self, kind: str, fresh: bool, scope: str, media_ids: list[int] | None, clear_cache: bool
    ) -> None:
        if kind in ("price", "cover"):
            await self._sweep(kind, fresh, scope, media_ids)
        elif kind == "reenrich":
            await self._reenrich()
        elif kind == "covers-reload":
            await self._covers_reload()
        elif kind == "rebuild-history":
            await self._rebuild()
        elif kind == "vacuum":
            await self._vacuum(clear_cache)
        else:
            raise ValueError(f"unknown maintenance kind {kind!r}")

    async def _targets(self, purpose: str, fresh: bool, scope: str, ids: list[int] | None) -> list[int]:
        """Reset the probe ledger for a purpose, then resolve the media list."""
        if fresh:
            async with self._db.write() as w:
                if ids is None:
                    await w.execute("DELETE FROM lookup_probes WHERE attempted_for = ?", (purpose,))
                else:
                    marks = ",".join("?" * len(ids))
                    await w.execute(
                        f"DELETE FROM lookup_probes WHERE attempted_for = ? AND media_id IN ({marks})",
                        (purpose, *ids),
                    )
        if ids is not None:
            return ids
        if scope == "missing":
            clause = (
                "effective_price_cents IS NULL OR price_basis IN ('unknown', 'default_by_class')"
                if purpose == "price"
                else "cover_sha256 IS NULL"
            )
            rows = await self._db.fetch_all(f"SELECT id FROM media WHERE {clause} ORDER BY id")
        else:
            rows = await self._db.fetch_all("SELECT id FROM media ORDER BY id")
        return [int(row["id"]) for row in rows]

    async def _sweep(self, purpose: str, fresh: bool, scope: str, ids: list[int] | None) -> None:
        if self._worker is None:
            raise RuntimeError("metadata enrichment is disabled")
        targets = await self._targets(purpose, fresh, scope, ids)
        self._status.total = len(targets)
        for media_id in targets:
            await self._worker.run_ladders(media_id)
            self._status.done += 1
        self._status.finished_at = datetime.now(UTC).isoformat()

    async def _reenrich(self) -> None:
        """Forget every provider record and probe, and walk the queue again.

        The media rows themselves stay; only the provider layer and its probe
        ledger are discarded, so manual prices and ratings are untouched.
        """
        async with self._db.write() as w:
            await w.execute("DELETE FROM metadata_records")
            await w.execute("DELETE FROM enrichment_jobs")
            await w.execute("DELETE FROM lookup_probes")
            await w.execute("UPDATE media SET metadata_state = 'pending'")
        if self._worker is not None:
            # Drain the queue now instead of waiting for the next scheduler
            # tick; run_once returns 0 when nothing is due.
            self._status.label = KIND_LABELS["reenrich"]
            while True:
                attempted = await self._worker.run_once(limit=20)
                self._status.done += attempted
                if attempted == 0:
                    break
        self._status.total = self._status.done
        self._status.finished_at = datetime.now(UTC).isoformat()

    async def _covers_reload(self) -> None:
        """Drop every stored cover and re-run the ladder for all works."""
        async with self._db.write() as w:
            await w.execute("DELETE FROM images")
            await w.execute("UPDATE media SET cover_sha256 = NULL, cover_aspect = NULL")
            await w.execute("DELETE FROM lookup_probes WHERE attempted_for = 'cover'")
        if self._worker is not None:
            rows = await self._db.fetch_all("SELECT id FROM media ORDER BY id")
            self._status.total = len(rows)
            for row in rows:
                await self._worker.run_ladders(int(row["id"]))
                self._status.done += 1
        self._status.finished_at = datetime.now(UTC).isoformat()

    async def _rebuild(self) -> None:
        from .library.reconcile import rebuild_history

        report: ReconcileReport = await rebuild_history(self._db, self._settings)
        self._status.detail = {
            "opened": report.opened,
            "closed": report.closed,
            "reopened": report.reopened,
            "renewed": report.renewed,
            "updated": report.updated,
        }
        self._status.finished_at = datetime.now(UTC).isoformat()

    async def _vacuum(self, clear_cache: bool) -> None:
        if clear_cache:
            async with self._db.write() as w:
                await w.execute("DELETE FROM http_cache")
        # sqlite3 cannot run VACUUM inside a transaction; the writer holds one.
        await asyncio.to_thread(self._vacuum_sync)
        self._status.finished_at = datetime.now(UTC).isoformat()

    def _vacuum_sync(self) -> None:
        import sqlite3

        conn = sqlite3.connect(self._db.path)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
