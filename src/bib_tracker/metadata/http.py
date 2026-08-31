"""HTTP for metadata providers: cached, rate-limited and polite.

These are free public services run by libraries and volunteers, so the rules
are theirs: identify yourself, do not hammer, honour Retry-After, and do not
ask the same question twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .. import __version__
from ..db.connection import Database

_LOGGER = logging.getLogger(__name__)

#: A negative answer is worth remembering for a while: an obscure German title
#: that Open Library has never heard of will still be unknown tomorrow.
NEGATIVE_TTL = timedelta(days=14)
POSITIVE_TTL = timedelta(days=30)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
#: BoardGameGeek answers 202 while it builds the response, and expects a retry.
QUEUED_STATUS = 202


class RateLimiter:
    """One token bucket per provider."""

    def __init__(self, per_minute: int) -> None:
        self._interval = 60.0 / max(1, per_minute)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval


@dataclass
class CachedResponse:
    status: int
    body: bytes
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class CachedClient:
    """Shared client with a per-provider limiter and an on-disk cache."""

    def __init__(
        self,
        db: Database,
        client: httpx.AsyncClient,
        *,
        contact: str = "https://github.com/makefu/bib-tracker",
        max_attempts: int = 5,
    ) -> None:
        self._db = db
        self._client = client
        self._limiters: dict[str, RateLimiter] = {}
        self._max_attempts = max_attempts
        self.user_agent = f"bib-tracker/{__version__} (+{contact})"

    def limiter(self, provider: str, per_minute: int) -> RateLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = RateLimiter(per_minute)
        return self._limiters[provider]

    async def get(
        self,
        url: str,
        *,
        provider: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        rate_limit_per_minute: int = 60,
        ttl: timedelta | None = None,
    ) -> CachedResponse:
        key = _cache_key(url, params)

        cached = await self._read_cache(key)
        if cached is not None:
            return cached

        await self.limiter(provider, rate_limit_per_minute).acquire()
        response = await self._fetch_with_backoff(url, params, headers, provider)

        # Errors are not cached: a 500 today says nothing about tomorrow,
        # unlike a 404, which usually does.
        if response.ok or response.status == 404:
            await self._write_cache(key, url, provider, response, ttl)
        return response

    async def _fetch_with_backoff(
        self,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        provider: str,
    ) -> CachedResponse:
        request_headers = {"User-Agent": self.user_agent, **(headers or {})}
        delay = 1.0

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(url, params=params, headers=request_headers)
            except httpx.HTTPError as err:
                if attempt == self._max_attempts:
                    _LOGGER.warning("%s: giving up on %s after %d attempts: %s", provider, url, attempt, err)
                    return CachedResponse(status=599, body=str(err).encode())
                await asyncio.sleep(_jittered(delay))
                delay *= 2
                continue

            if response.status_code not in RETRY_STATUSES and response.status_code != QUEUED_STATUS:
                return CachedResponse(status=response.status_code, body=response.content)

            if attempt == self._max_attempts:
                return CachedResponse(status=response.status_code, body=response.content)

            wait = _retry_after(response) or _jittered(delay)
            _LOGGER.info("%s: %s, retrying in %.1fs", provider, response.status_code, wait)
            await asyncio.sleep(wait)
            delay *= 2

        raise AssertionError("unreachable")

    async def _read_cache(self, key: str) -> CachedResponse | None:
        row = await self._db.fetch_one("SELECT status, body, expires_at FROM http_cache WHERE url_hash = ?", (key,))
        if row is None:
            return None
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return None
        return CachedResponse(status=row["status"], body=row["body"] or b"", from_cache=True)

    async def _write_cache(
        self,
        key: str,
        url: str,
        provider: str,
        response: CachedResponse,
        ttl: timedelta | None,
    ) -> None:
        lifetime = ttl or (POSITIVE_TTL if response.ok else NEGATIVE_TTL)
        async with self._db.write() as w:
            await w.execute(
                """
                INSERT INTO http_cache (url_hash, url, provider, status, body, fetched_at, expires_at)
                VALUES (:key, :url, :provider, :status, :body, :fetched_at, :expires_at)
                ON CONFLICT (url_hash) DO UPDATE SET
                    status = excluded.status, body = excluded.body,
                    fetched_at = excluded.fetched_at, expires_at = excluded.expires_at
                """,
                {
                    "key": key,
                    "url": url,
                    "provider": provider,
                    "status": response.status,
                    "body": response.body,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + lifetime).isoformat(),
                },
            )


def _cache_key(url: str, params: dict[str, Any] | None) -> str:
    canonical = url
    if params:
        canonical += "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _jittered(delay: float) -> float:
    """Full jitter, so several jobs backing off do not resynchronise."""
    return random.uniform(0, delay)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
