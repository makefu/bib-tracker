"""Periodic polling.

APScheduler rather than a bare asyncio loop: coalescing and misfire grace mean
a host that was suspended over six poll windows produces one catch-up run
instead of six, and jitter keeps several accounts from hitting their libraries
in the same second. Re-implementing those correctly is most of the work a
hand-rolled loop would save.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .metadata.worker import EnrichmentWorker
from .services import PollService

_LOGGER = logging.getLogger(__name__)


class PollScheduler:
    def __init__(
        self,
        service: PollService,
        settings: Settings,
        enrichment: EnrichmentWorker | None = None,
    ) -> None:
        self._service = service
        self._settings = settings
        self._enrichment = enrichment
        # In-memory only: the schedule is derived from declarative config on
        # every boot, so persisting it would just let it go stale.
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        for account in self._service.accounts:
            if not account.enabled:
                continue
            self._scheduler.add_job(
                self._poll,
                trigger=IntervalTrigger(
                    minutes=self._settings.poll_interval_minutes,
                    jitter=self._settings.poll_jitter_seconds,
                ),
                args=[account.name],
                id=f"poll:{account.name}",
                name=f"Poll {account.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
                replace_existing=True,
            )
        if self._enrichment is not None:
            # Short interval, small batches: enrichment is best-effort and must
            # never hold up a poll or the web server.
            self._scheduler.add_job(
                self._enrich,
                trigger=IntervalTrigger(seconds=60),
                id="enrichment",
                name="Metadata enrichment",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                replace_existing=True,
            )

        self._scheduler.start()
        _LOGGER.info(
            "Scheduled %d account(s) every %d minutes",
            len(self._scheduler.get_jobs()),
            self._settings.poll_interval_minutes,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _enrich(self) -> None:
        if self._enrichment is None:
            return
        try:
            await self._enrichment.run_once()
        except Exception:
            _LOGGER.exception("Metadata enrichment failed")

    async def _poll(self, name: str) -> None:
        try:
            await self._service.poll(name, trigger="schedule")
        except Exception:
            # Never let a job raise into APScheduler: one bad account must not
            # stop the others from being scheduled.
            _LOGGER.exception("Scheduled poll of %s failed", name)
