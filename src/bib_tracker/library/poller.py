"""Fetch one account's current state from its OPAC.

Wraps ha_stadtbibliothek's backends and turns every failure into a classified
outcome rather than an exception, because the caller has to record *why* a poll
failed: an authentication failure must never be mistaken for an empty account.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from custom_components.stadtbibliothek.backends import create_backend
from custom_components.stadtbibliothek.backends.base import (
    AuthenticationError,
    FeeItem,
    LibraryBackend,
    LoanItem,
    ParseError,
)
from custom_components.stadtbibliothek.serializers import serialize_fee, serialize_loan

from ..config import AccountConfig

_LOGGER = logging.getLogger(__name__)


class PollStatus(StrEnum):
    SUCCESS = "success"
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    INTERNAL_ERROR = "internal_error"


@dataclass
class PollResult:
    status: PollStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    loans: list[LoanItem] = field(default_factory=list)
    fees: list[FeeItem] = field(default_factory=list)
    fees_supported: bool = True
    error_kind: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is PollStatus.SUCCESS

    def serialised_loans(self) -> list[dict[str, Any]]:
        return [serialize_loan(loan) for loan in self.loans]

    def serialised_fees(self) -> list[dict[str, Any]]:
        return [serialize_fee(fee) for fee in self.fees]


async def poll_account(
    account: AccountConfig,
    password: str,
    *,
    client: httpx.AsyncClient | None = None,
    fetch_details: bool = False,
) -> PollResult:
    """Log in, read the account, and report what happened.

    Never raises: a poll that blew up is a recorded fact, not a caller problem.
    """
    started = datetime.now(UTC)
    monotonic_start = time.monotonic()

    def finish(
        status: PollStatus,
        *,
        loans: list[LoanItem] | None = None,
        fees: list[FeeItem] | None = None,
        fees_supported: bool = True,
        error: BaseException | None = None,
    ) -> PollResult:
        return PollResult(
            status=status,
            started_at=started,
            finished_at=datetime.now(UTC),
            duration_ms=int((time.monotonic() - monotonic_start) * 1000),
            loans=loans or [],
            fees=fees or [],
            fees_supported=fees_supported,
            error_kind=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )

    try:
        backend = await create_backend(account.library_type, client=client, base_url=account.base_url)
    except Exception as err:
        return finish(PollStatus.INTERNAL_ERROR, error=err)

    try:
        await backend.login(account.username, password)
        loans = await backend.get_loans()
        fees = await backend.get_fees() if backend.supports_fees else []

        if fetch_details and backend.supports_details:
            loans = [await _details(backend, loan) for loan in loans]

        return finish(PollStatus.SUCCESS, loans=loans, fees=fees, fees_supported=backend.supports_fees)
    except AuthenticationError as err:
        return finish(PollStatus.AUTH_ERROR, error=err)
    except ParseError as err:
        return finish(PollStatus.PARSE_ERROR, error=err)
    except (httpx.HTTPError, TimeoutError, OSError) as err:
        return finish(PollStatus.NETWORK_ERROR, error=err)
    except Exception as err:
        _LOGGER.exception("Unexpected failure polling account %s", account.name)
        return finish(PollStatus.INTERNAL_ERROR, error=err)
    finally:
        await backend.close()


async def _details(backend: LibraryBackend, loan: LoanItem) -> LoanItem:
    """Enrichment is best-effort: one bad detail page must not fail the poll."""
    try:
        return await backend.fetch_details(loan)
    except Exception as err:
        _LOGGER.warning("Could not fetch details for %r: %s", loan.title, err)
        return loan
