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
from typing import Any, Protocol

import httpx
from custom_components.stadtbibliothek.backends import create_backend
from custom_components.stadtbibliothek.backends.base import (
    AuthenticationError,
    FeeItem,
    LibraryBackend,
    LoanItem,
    ParseError,
    RateLimitedError,
)
from custom_components.stadtbibliothek.serializers import serialize_fee, serialize_loan

from ..config import AccountConfig

_LOGGER = logging.getLogger(__name__)


class DetailsGate(Protocol):
    """Per-loan decision on fetching the OPAC's detail page, owned by the caller.

    One detail page is one request per loan, which is the traffic pattern the
    Stuttgart firewall punishes. The gate is the caller's cache: it applies
    what an earlier poll already learned and returns only the loans still
    worth asking about.
    """

    async def prepare(self, loans: list[LoanItem]) -> list[LoanItem]:
        """Apply cached enrichment in place; return the loans needing a fetch."""
        ...

    async def record(self, loan: LoanItem) -> None:
        """Persist what a fetched detail page said."""
        ...


class PollStatus(StrEnum):
    SUCCESS = "success"
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    INTERNAL_ERROR = "internal_error"
    #: The OPAC turned us away (403/429). Distinct from a network error so the
    #: scheduler can back off for hours instead of retrying every interval.
    RATE_LIMITED = "rate_limited"


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
    #: Seconds the site asked us to wait (Retry-After), when it said.
    retry_after: float | None = None

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
    details_gate: DetailsGate | None = None,
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
            retry_after=error.retry_after if isinstance(error, RateLimitedError) else None,
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
            loans = await _details(backend, loans, details_gate)

        return finish(PollStatus.SUCCESS, loans=loans, fees=fees, fees_supported=backend.supports_fees)
    except AuthenticationError as err:
        return finish(PollStatus.AUTH_ERROR, error=err)
    except ParseError as err:
        return finish(PollStatus.PARSE_ERROR, error=err)
    except RateLimitedError as err:
        return finish(PollStatus.RATE_LIMITED, error=err)
    except (httpx.HTTPError, TimeoutError, OSError) as err:
        return finish(PollStatus.NETWORK_ERROR, error=err)
    except Exception as err:
        _LOGGER.exception("Unexpected failure polling account %s", account.name)
        return finish(PollStatus.INTERNAL_ERROR, error=err)
    finally:
        await backend.close()


async def _details(
    backend: LibraryBackend,
    loans: list[LoanItem],
    gate: DetailsGate | None,
) -> list[LoanItem]:
    """Enrichment is best-effort: one bad detail page must not fail the poll.

    The gate decides which loans still need a request — without it every poll
    would fetch a detail page per loan, which is what the Stuttgart firewall
    punishes. A 403 from a detail page must still reach the caller: it is the
    same site blocking us, not one loan's page being broken.
    """
    pending = list(loans) if gate is None else await gate.prepare(loans)
    for loan in pending:
        enriched = loan
        try:
            # The loan list is mutated in place; index by identity, since two
            # identical exemplars would compare equal.
            index = next(i for i, item in enumerate(loans) if item is loan)
            enriched = loans[index] = await backend.fetch_details(loan)
        except RateLimitedError:
            raise
        except Exception as err:
            _LOGGER.warning("Could not fetch details for %r: %s", loan.title, err)
        if gate is not None:
            await gate.record(enriched)
    return loans
