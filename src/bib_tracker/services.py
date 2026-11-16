"""Poll orchestration.

Owns the locking, the concurrency limit, and the decision of whether a poll's
result may be trusted enough to become history.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from custom_components.stadtbibliothek.backends import create_backend
from custom_components.stadtbibliothek.backends.base import LoanItem

from .config import AccountConfig, Settings
from .db import queries
from .db.connection import Database
from .library.identity import copy_key
from .library.poller import DetailsGate, PollResult, PollStatus, poll_account
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

    def account(self, name: str) -> AccountConfig | None:
        return self._accounts.get(name)

    def shared_client(self) -> httpx.AsyncClient:
        return self._shared_client()

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

    async def poll(self, name: str, *, trigger: str = "manual") -> int | None:
        """Poll one account and record the outcome. Returns the run id.

        A scheduled or startup poll honours the account's back-off and
        returns None instead of waking a firewall that asked us to go away;
        a manual poll is the operator's deliberate click and always runs.
        """
        account = self._accounts.get(name)
        if account is None:
            raise KeyError(f"Unknown account {name!r}")

        lock = self._locks[name]
        if lock.locked():
            raise PollInProgressError(name)

        async with lock, self._semaphore:
            if trigger in {"schedule", "startup"} and await self._deferred(name):
                return None
            return await self._poll_locked(account, trigger)

    async def _deferred(self, name: str) -> bool:
        """True while the account's site is still telling us to wait."""
        account_id = await queries.account_id_by_name(self._db, name)
        if account_id is None:
            return False
        state = await queries.get_backoff(self._db, account_id)
        if state.until is None or state.until <= datetime.now(UTC):
            return False
        _LOGGER.info(
            "poll of %s deferred until %s (%d consecutive failures)",
            name,
            state.until.isoformat(),
            state.failures,
        )
        return True

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

        gate = (
            _DetailCache(
                self._db,
                row.id,
                account.library_type,
                max_age_days=self._settings.details_retry_days,
            )
            if self._settings.metadata_enabled
            else None
        )
        result = await poll_account(
            account,
            password,
            client=self._shared_client(),
            fetch_details=self._settings.metadata_enabled,
            details_gate=gate,
        )
        await self._record(run_id, row.id, account, result)
        return run_id

    async def _record(self, run_id: int, account_id: int, account: AccountConfig, result: PollResult) -> None:
        if not result.ok:
            # A failed poll writes no snapshot at all, so nothing downstream
            # can ever read it as "the account was empty".
            _LOGGER.warning(
                "poll of %s failed: %s (%s: %s)",
                account.name,
                result.status.value,
                result.error_kind,
                result.error_message,
            )
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
            if result.status is PollStatus.RATE_LIMITED:
                until = await queries.bump_backoff(
                    self._db,
                    account_id,
                    base_minutes=self._settings.poll_backoff_base_minutes,
                    max_hours=self._settings.poll_backoff_max_hours,
                    retry_after=result.retry_after,
                )
                _LOGGER.warning(
                    "account %s was rate limited; not polling it again before %s",
                    account.name,
                    until.isoformat(),
                )
            return

        await queries.clear_backoff(self._db, account_id)

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


class _DetailCache(DetailsGate):
    """Gates the per-loan ``fetch_details`` requests against ``detail_lookups``.

    One detail page is one request per loan, which is the traffic pattern the
    Stuttgart firewall punishes. A copy is asked about once per
    ``details_retry_days``, and the answer is applied from the ledger on every
    other poll. A lookup that found nothing is cached as nothing — that is
    the whole point: a title the catalogue has no jacket for must not cost a
    request on each poll.
    """

    def __init__(self, db: Database, account_id: int, library_type: str, *, max_age_days: int) -> None:
        self._db = db
        self._account_id = account_id
        self._library_type = library_type
        self._max_age_days = max_age_days

    async def prepare(self, loans: list[LoanItem]) -> list[LoanItem]:
        cached = await queries.get_detail_lookups(self._db, self._account_id, max_age_days=self._max_age_days)
        pending: list[LoanItem] = []
        for loan in loans:
            entry = cached.get(self._key(loan))
            if entry is None:
                pending.append(loan)
                continue
            # apply what an earlier poll already learned; the listing itself
            # may have covered a field more recently than the ledger did
            if entry["cover_url"] and not loan.cover_url:
                loan.cover_url = entry["cover_url"]
            if entry["isbn"] and not loan.isbn:
                loan.isbn = entry["isbn"]
        return pending

    async def record(self, loan: LoanItem) -> None:
        await queries.store_detail_lookup(
            self._db,
            self._account_id,
            self._key(loan),
            cover_url=loan.cover_url,
            isbn=loan.isbn,
        )

    def _key(self, loan: LoanItem) -> str:
        return copy_key(
            self._library_type,
            title=loan.title,
            item_id=loan.item_id or None,
            barcode=loan.barcode,
            call_number=loan.call_number,
            author=loan.author,
            media_type=loan.media_type,
        )


@dataclass
class RenewalOutcome:
    loan_key: str
    title: str
    item_id: str
    success: bool
    error: str | None = None


class RenewalService:
    """Renewing loans.

    Not what this application is for -- Home Assistant does the routine
    renewing -- so this exists for the occasional deliberate click, and always
    re-polls afterwards rather than guessing at the new due date.
    """

    def __init__(self, db: Database, settings: Settings, polling: PollService) -> None:
        self._db = db
        self._settings = settings
        self._polling = polling

    async def renew_loan(self, loan_key: str) -> RenewalOutcome:
        row = await self._db.fetch_one(
            """
            SELECT l.loan_key, m.title, c.item_id, a.name AS account
            FROM loans l
            JOIN media m ON m.id = l.media_id
            JOIN copies c ON c.id = l.copy_id
            JOIN accounts a ON a.id = l.account_id
            WHERE l.loan_key = ? AND l.state = 'open'
            """,
            (loan_key,),
        )
        if row is None:
            raise KeyError(f"No open loan {loan_key!r}")

        outcomes = await self._renew(row["account"], [dict(row)])
        return outcomes[0]

    async def renew_due(self, account_name: str, threshold_days: int | None = None) -> list[RenewalOutcome]:
        """Renew everything falling due within the threshold."""
        # Validate before querying: an unknown account must be an error, not
        # an empty result that looks like "nothing was due".
        if self._polling.account(account_name) is None:
            raise KeyError(f"Unknown account {account_name!r}")

        threshold = self._settings.renew_threshold_days if threshold_days is None else threshold_days
        rows = await self._db.fetch_all(
            """
            SELECT l.loan_key, m.title, c.item_id
            FROM loans l
            JOIN media m ON m.id = l.media_id
            JOIN copies c ON c.id = l.copy_id
            JOIN accounts a ON a.id = l.account_id
            WHERE l.state = 'open' AND a.name = ? AND l.can_be_renewed = 1
                  AND julianday(l.last_due_date) - julianday(date('now')) <= ?
            ORDER BY l.last_due_date
            """,
            (account_name, threshold),
        )
        return await self._renew(account_name, [dict(row) for row in rows])

    async def _renew(self, account_name: str, loans: list[dict[str, Any]]) -> list[RenewalOutcome]:
        account = self._polling.account(account_name)
        if account is None:
            raise KeyError(f"Unknown account {account_name!r}")
        if not loans:
            return []

        backend = await create_backend(
            account.library_type,
            client=self._polling.shared_client(),
            base_url=account.base_url,
        )
        outcomes: list[RenewalOutcome] = []
        try:
            await backend.login(account.username, account.resolve_password())
            for entry in loans:
                outcomes.append(await self._renew_one(backend, entry))
        except Exception as err:
            outcomes.extend(
                RenewalOutcome(
                    loan_key=entry["loan_key"],
                    title=entry["title"],
                    item_id=entry["item_id"] or "",
                    success=False,
                    error=str(err),
                )
                for entry in loans[len(outcomes) :]
            )
        finally:
            await backend.close()

        if any(outcome.success for outcome in outcomes):
            # Re-poll rather than assume the new due date: the library decides
            # how long an extension runs, and it is not always a full period.
            with contextlib.suppress(PollInProgressError):
                await self.poll_after_renewal(account_name)

        return outcomes

    async def poll_after_renewal(self, account_name: str) -> int | None:
        return await self._polling.poll(account_name, trigger="manual")

    @staticmethod
    async def _renew_one(backend: Any, entry: dict[str, Any]) -> RenewalOutcome:
        item_id = entry["item_id"] or ""
        try:
            ok = await backend.renew_loan(item_id)
        except Exception as err:
            return RenewalOutcome(entry["loan_key"], entry["title"], item_id, False, str(err))
        return RenewalOutcome(
            entry["loan_key"],
            entry["title"],
            item_id,
            bool(ok),
            None if ok else "Die Bibliothek hat die Verlängerung abgelehnt",
        )
