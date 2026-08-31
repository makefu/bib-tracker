"""Enrichment: ask the providers about works we have not resolved yet.

Runs as a queue rather than inline with polling, so a slow or unreachable
provider delays nothing that matters and a failure can be retried later.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import Settings
from ..db.connection import Database
from ..library.media_class import MediaClass
from ..library.merge_media import adopt_isbn
from . import PROVIDER_FACTORIES, build_provider
from .base import BaseProvider, MediaQuery, ProviderConfig, ProviderRecord, ProviderStatus
from .covers import store_cover
from .http import CachedClient
from .matcher import choose
from .merge import merge_records
from .pricing import preferred_provider_price, store_price

_LOGGER = logging.getLogger(__name__)

#: How long before a provider is asked about a work again.
REFRESH_AFTER = timedelta(days=90)
#: Give up after this many failures, so one broken record cannot spin forever.
MAX_ATTEMPTS = 5
BACKOFF = timedelta(hours=6)


class EnrichmentWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> None:
        self._db = db
        self._settings = settings
        self._http = CachedClient(db, client, contact=settings.user_agent_contact)
        self._providers = self._build_providers()

    def _build_providers(self) -> dict[str, BaseProvider]:
        providers: dict[str, BaseProvider] = {}
        for name in self._settings.metadata_providers:
            if name not in PROVIDER_FACTORIES:
                _LOGGER.warning("Unknown metadata provider %r; ignoring", name)
                continue
            config = ProviderConfig(
                name=name,
                base_url=self._settings.metadata_base_urls.get(name),
                api_key=self._settings.provider_api_key(name),
                rate_limit_per_minute=self._settings.provider_rate_limit(name),
            )
            providers[name] = build_provider(config, self._http)
        return providers

    def usable_providers(self) -> dict[str, BaseProvider]:
        """Those actually able to answer: configured, and holding any key they need."""
        return {name: p for name, p in self._providers.items() if p.available()}

    def unusable_providers(self) -> dict[str, str]:
        """Configured but unavailable, with why -- so the interface can say so."""
        return {
            name: "braucht Zugangsdaten" if p.requires_credentials else "deaktiviert"
            for name, p in self._providers.items()
            if not p.available()
        }

    async def enqueue_pending(self) -> int:
        """Queue every provider that could say something about each new work."""
        rows = await self._db.fetch_all(
            "SELECT id, media_class FROM media WHERE metadata_state IN ('pending', 'failed')"
        )
        queued = 0
        async with self._db.write() as w:
            for row in rows:
                media_class = MediaClass(row["media_class"])
                for name, provider in self.usable_providers().items():
                    if not provider.supports_class(media_class):
                        continue
                    await w.execute(
                        """
                        INSERT INTO enrichment_jobs (media_id, provider, state, next_attempt_at)
                        VALUES (?, ?, 'queued', datetime('now'))
                        ON CONFLICT (media_id, provider) DO NOTHING
                        """,
                        (row["id"], name),
                    )
                    queued += 1
        return queued

    async def run_once(self, limit: int = 20) -> int:
        """Work through due jobs. Returns how many were attempted."""
        await self.enqueue_pending()

        rows = await self._db.fetch_all(
            """
            SELECT j.id, j.media_id, j.provider, j.attempts, m.title, m.author,
                   m.media_class, m.isbn13, m.publisher, m.published_year
            FROM enrichment_jobs j JOIN media m ON m.id = j.media_id
            WHERE j.state = 'queued' AND j.next_attempt_at <= datetime('now')
            ORDER BY j.next_attempt_at LIMIT ?
            """,
            (limit,),
        )

        for row in rows:
            await self._run_job(dict(row))

        if rows:
            await self._apply_results({row["media_id"] for row in rows})
        return len(rows)

    async def _run_job(self, job: dict[str, Any]) -> None:
        provider = self._providers.get(job["provider"])
        if provider is None or not provider.available():
            await self._finish_job(job["id"], "skipped")
            return

        query = MediaQuery(
            media_class=MediaClass(job["media_class"]),
            title=job["title"],
            author=job["author"],
            isbn=job["isbn13"],
            publisher=job["publisher"],
            published_year=job["published_year"],
        )

        try:
            record = await self._lookup(provider, query)
        except Exception as err:
            _LOGGER.warning("%s failed for %r: %s", provider.name, job["title"], err)
            await self._defer_job(job, str(err))
            return

        if record is None:
            await self._store_record(
                job["media_id"],
                ProviderRecord(
                    provider=provider.name,
                    external_id="",
                    status=ProviderStatus.NOT_FOUND,
                ),
                confirmed=True,
            )
            await self._finish_job(job["id"], "done")
            return

        if record.status is ProviderStatus.ERROR:
            await self._defer_job(job, "provider error")
            return

        await self._store_record(job["media_id"], record, confirmed=not record.needs_confirmation)
        await self._finish_job(job["id"], "done")

    async def _lookup(self, provider: BaseProvider, query: MediaQuery) -> ProviderRecord | None:
        """Resolve a work to one provider record, or nothing."""
        if query.isbn and provider.name == "openlibrary":
            direct = await provider.fetch(f"isbn:{query.isbn}")
            if direct is not None and direct.status is ProviderStatus.OK:
                direct.match_method = "isbn"
                direct.match_confidence = 1.0
                return direct

        candidates = await provider.search(query)
        if not candidates:
            return None

        best, needs_confirmation = choose(query, candidates)
        if best is None:
            return None

        record = await provider.fetch(best.external_id)
        if record is None:
            return None

        record.match_confidence = best.score
        record.match_method = "isbn" if query.isbn and best.isbn13 == query.isbn else "title_author"
        record.needs_confirmation = needs_confirmation
        return record

    async def _store_record(self, media_id: int, record: ProviderRecord, *, confirmed: bool) -> None:
        async with self._db.write() as w:
            await w.execute(
                """
                INSERT INTO metadata_records (
                    media_id, provider, external_id, external_url, status, match_method,
                    match_confidence, confirmed, title, authors, isbn13, published_year,
                    publisher, page_count, language, description, rating_value, rating_scale,
                    rating_count, list_price_cents, list_price_currency, cover_source_url,
                    payload_json, fetched_at, refresh_after
                ) VALUES (
                    :media_id, :provider, :external_id, :external_url, :status, :match_method,
                    :match_confidence, :confirmed, :title, :authors, :isbn13, :published_year,
                    :publisher, :page_count, :language, :description, :rating_value, :rating_scale,
                    :rating_count, :list_price_cents, :list_price_currency, :cover_source_url,
                    :payload_json, :fetched_at, :refresh_after
                )
                ON CONFLICT (media_id, provider) DO UPDATE SET
                    external_id = excluded.external_id, external_url = excluded.external_url,
                    status = excluded.status, match_method = excluded.match_method,
                    match_confidence = excluded.match_confidence, confirmed = excluded.confirmed,
                    title = excluded.title, authors = excluded.authors, isbn13 = excluded.isbn13,
                    published_year = excluded.published_year, publisher = excluded.publisher,
                    page_count = excluded.page_count, language = excluded.language,
                    description = excluded.description, rating_value = excluded.rating_value,
                    rating_scale = excluded.rating_scale, rating_count = excluded.rating_count,
                    list_price_cents = excluded.list_price_cents,
                    list_price_currency = excluded.list_price_currency,
                    cover_source_url = excluded.cover_source_url,
                    payload_json = excluded.payload_json, fetched_at = excluded.fetched_at,
                    refresh_after = excluded.refresh_after
                """,
                {
                    "media_id": media_id,
                    "provider": record.provider,
                    "external_id": record.external_id,
                    "external_url": record.external_url,
                    "status": record.status.value,
                    "match_method": record.match_method,
                    "match_confidence": record.match_confidence,
                    "confirmed": int(confirmed),
                    "title": record.title,
                    "authors": json.dumps(record.authors, ensure_ascii=False),
                    "isbn13": record.isbn13,
                    "published_year": record.published_year,
                    "publisher": record.publisher,
                    "page_count": record.page_count,
                    "language": record.language,
                    "description": record.description,
                    "rating_value": record.rating_value,
                    "rating_scale": record.rating_scale,
                    "rating_count": record.rating_count,
                    "list_price_cents": record.list_price_cents,
                    "list_price_currency": record.list_price_currency,
                    "cover_source_url": record.cover_source_url,
                    "payload_json": json.dumps(record.payload, ensure_ascii=False, default=str),
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "refresh_after": (datetime.now(UTC) + REFRESH_AFTER).isoformat(),
                },
            )

    async def _finish_job(self, job_id: int, state: str) -> None:
        async with self._db.write() as w:
            await w.execute(
                "UPDATE enrichment_jobs SET state = ?, updated_at = datetime('now') WHERE id = ?",
                (state, job_id),
            )

    async def _defer_job(self, job: dict[str, Any], error: str) -> None:
        attempts = int(job["attempts"]) + 1
        state = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        async with self._db.write() as w:
            await w.execute(
                """
                UPDATE enrichment_jobs SET state = ?, attempts = ?, last_error = ?,
                       next_attempt_at = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (state, attempts, error, (datetime.now(UTC) + BACKOFF).isoformat(), job["id"]),
            )

    async def _apply_results(self, media_ids: set[int]) -> None:
        """Fold each work's provider records into the media row."""
        for media_id in media_ids:
            rows = await self._db.fetch_all(
                "SELECT * FROM metadata_records WHERE media_id = ? AND confirmed = 1",
                (media_id,),
            )
            records = [_to_record(row) for row in rows]
            merged = merge_records(records)

            unconfirmed = await self._db.fetch_one(
                "SELECT COUNT(*) AS n FROM metadata_records WHERE media_id = ? AND confirmed = 0",
                (media_id,),
            )
            state = "needs_confirmation" if unconfirmed and unconfirmed["n"] else ("enriched" if merged else "failed")

            await self._write_media(media_id, merged, state)

            # Prices follow their own preference order, not the general
            # merge precedence: which source is authoritative for a German
            # retail price is a different question from who has the best
            # description.
            price = await preferred_provider_price(self._db, self._settings, media_id)
            if price is not None:
                await store_price(self._db, media_id, price, source="provider")

            cover_url = merged.get("cover_source_url")
            if cover_url:
                await store_cover(self._db, self._http, media_id, str(cover_url))

    async def _write_media(self, media_id: int, merged: dict[str, Any], state: str) -> None:
        columns = {
            key: value
            for key, value in merged.items()
            if key in {"published_year", "publisher", "page_count", "language", "description", "author"}
        }
        assignments = ", ".join(f"{key} = :{key}" for key in columns)
        async with self._db.write() as w:
            isbn = merged.get("isbn13")
            if isbn:
                # Two catalogue records resolving to one ISBN are one work.
                media_id = await adopt_isbn(w, media_id, str(isbn))
            await w.execute(
                f"UPDATE media SET {assignments + ', ' if assignments else ''}"
                "metadata_state = :state, updated_at = datetime('now') WHERE id = :id",
                {**columns, "state": state, "id": media_id},
            )


def _to_record(row: Any) -> ProviderRecord:
    return ProviderRecord(
        provider=row["provider"],
        external_id=row["external_id"] or "",
        status=ProviderStatus(row["status"]),
        external_url=row["external_url"],
        title=row["title"],
        authors=json.loads(row["authors"] or "[]"),
        isbn13=row["isbn13"],
        published_year=row["published_year"],
        publisher=row["publisher"],
        page_count=row["page_count"],
        language=row["language"],
        description=row["description"],
        rating_value=row["rating_value"],
        rating_scale=row["rating_scale"],
        rating_count=row["rating_count"],
        list_price_cents=row["list_price_cents"],
        list_price_currency=row["list_price_currency"],
        cover_source_url=row["cover_source_url"],
    )
