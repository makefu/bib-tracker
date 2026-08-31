"""Poll orchestration.

Owns the locking, the concurrency limit, and the decision of whether a poll's
result may be trusted enough to become history.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import AccountConfig, Settings
from .db import queries
from .db.connection import Database
from .library.identity import copy_key
from .library.poller import PollResult, PollStatus, poll_account
from .library.reconcile import reconcile_pending

_LOGGER = logging.getLogger(__name__)


class PollInProgressError(RuntimeError):
    """That account is already being polled."""

    def __init__(self, account: str) -> None:
        super().__init__(f"A poll of account {account!r} is already running")
        self.account = account


class PollService:
    def __init__(self, db: Database, settings: Settings, accounts: list[AccountConfig]) -> None:
        self._db = db
        self._settings = settings
        self._accounts = {account.name: account for account in accounts}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._semaphore = asyncio.Semaphore(settings.poll_max_concurrent)
        self._client: httpx.AsyncClient | None = None

    @property
    def accounts(self) -> list[AccountConfig]:
        return list(self._accounts.values())

    def is_running(self, name: str) -> bool:
        return self._locks[name].locked()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _shared_client(self) -> httpx.AsyncClient:
        # One client for every account: connection reuse, one place to put a
        # timeout. Safe now that backends only close clients they opened.
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        return self._client

    async def poll(self, name: str, *, trigger: str = "manual") -> int:
        """Poll one account and record the outcome. Returns the run id."""
        account = self._accounts.get(name)
        if account is None:
            raise KeyError(f"Unknown account {name!r}")

        lock = self._locks[name]
        if lock.locked():
            raise PollInProgressError(name)

        async with lock, self._semaphore:
            return await self._poll_locked(account, trigger)

    async def poll_all(self, *, trigger: str = "schedule") -> dict[str, int | None]:
        """Poll every enabled account. One failure never affects the others."""

        async def one(account: AccountConfig) -> tuple[str, int | None]:
            try:
                return account.name, await self.poll(account.name, trigger=trigger)
            except PollInProgressError:
                return account.name, None
            except Exception:
                _LOGGER.exception("Polling account %s failed unexpectedly", account.name)
                return account.name, None

        enabled = [a for a in self._accounts.values() if a.enabled]
        results = await asyncio.gather(*(one(account) for account in enabled))
        return dict(results)

    async def _poll_locked(self, account: AccountConfig, trigger: str) -> int:
        row = await queries.get_account(self._db, account.name)
        if row is None:
            raise KeyError(f"Account {account.name!r} is not in the database")

        started = datetime.now(UTC)
        run_id = await queries.start_run(self._db, row.id, trigger, started)

        try:
            password = account.resolve_password()
        except Exception as err:
            await queries.finish_run(
                self._db,
                run_id,
                status=PollStatus.INTERNAL_ERROR.value,
                finished_at=datetime.now(UTC),
                duration_ms=0,
                error_kind=type(err).__name__,
                error_message=str(err),
                account_id=row.id,
            )
            return run_id

        result = await poll_account(
            account,
            password,
            client=self._shared_client(),
            fetch_details=self._settings.metadata_enabled,
        )
        await self._record(run_id, row.id, account, result)
        return run_id

    async def _record(self, run_id: int, account_id: int, account: AccountConfig, result: PollResult) -> None:
        if not result.ok:
            # A failed poll writes no snapshot at all, so nothing downstream
            # can ever read it as "the account was empty".
            await queries.finish_run(
                self._db,
                run_id,
                status=result.status.value,
                finished_at=result.finished_at,
                duration_ms=result.duration_ms,
                error_kind=result.error_kind,
                error_message=result.error_message,
                account_id=account_id,
            )
            return

        items = [{"copy_key": self._copy_key(account, raw), "raw": raw} for raw in result.serialised_loans()]
        status, reason = await self._assess(account_id, len(items))

        if status == "success":
            # A later poll agreeing with the held-back ones settles it. They
            # are promoted rather than dropped, so the history keeps the date
            # the change was first seen rather than when it was confirmed.
            promoted = await queries.promote_suspect_runs(self._db, account_id)
            if promoted:
                _LOGGER.info("Confirmed %d held-back poll(s) for %s", promoted, account.name)

        await queries.finish_run(
            self._db,
            run_id,
            status=status,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            loan_count=len(items),
            fee_count=len(result.fees),
            fees_supported=result.fees_supported,
            suspect_reason=reason,
            snapshot=items,
            account_id=account_id,
            observed_at=result.finished_at,
        )

        if status == "success":
            await reconcile_pending(self._db, self._settings, self._accounts)

    @staticmethod
    def _copy_key(account: AccountConfig, raw: dict[str, Any]) -> str:
        return copy_key(
            account.library_type,
            title=raw["title"],
            item_id=raw.get("item_id"),
            barcode=raw.get("barcode"),
            call_number=raw.get("call_number"),
            author=raw.get("author"),
            media_type=raw.get("media_type"),
        )

    async def _assess(self, account_id: int, loan_count: int) -> tuple[str, str | None]:
        """Decide whether this result is believable enough to become history.

        Both scrapers return an empty list when their table selector misses, so
        a sudden emptiness is at least as likely to be a broken parser as a
        genuine return. Holding such a run back costs one poll interval when
        the emptiness is real, and saves the entire history when it is not.
        """
        previous = await queries.last_successful_loan_count(self._db, account_id)
        if previous is None or previous == 0:
            return "success", None

        if loan_count == 0:
            confirmations = await queries.consecutive_suspect_runs(self._db, account_id)
            if confirmations + 1 < self._settings.zero_result_confirmations:
                return "suspect", f"all {previous} loans vanished at once; awaiting confirmation"
            return "success", None

        # Returning most of a small pile in one trip is ordinary, so this
        # rarely fires for a household-sized account -- there the zero-result
        # rule above is the real guard. It earns its keep on larger accounts,
        # where losing most of the list at once is not a plausible errand.
        dropped = previous - loan_count
        if dropped >= 3 and dropped / previous >= self._settings.suspect_drop_ratio:
            confirmations = await queries.consecutive_suspect_runs(self._db, account_id)
            if confirmations + 1 < self._settings.zero_result_confirmations:
                return "suspect", f"{dropped} of {previous} loans vanished at once; awaiting confirmation"

        return "success", None
