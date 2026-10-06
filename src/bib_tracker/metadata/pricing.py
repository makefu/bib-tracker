"""What a borrowed item would have cost to buy.

Three sources, in strict order. A price you entered always wins: you can see
the book, and no lookup outranks that. Then a provider's list price. Then a
configured default for the media class, which is a guess and is labelled as
one everywhere it is used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..db.connection import Database

if TYPE_CHECKING:
    from .base import MediaQuery
    from .http import CachedClient

_LOGGER = logging.getLogger(__name__)

#: A lone run of dots between 1-3 digit groups is a thousands separator
#: ("1.234"), the way a price is written on a German shelf; a dot with a
#: 1, 2 or 4-digit group on either side is the decimal point ("12.34").
_THOUSANDS = re.compile(r"\d{1,3}(\.\d{3})+$")


class PriceBasis(StrEnum):
    MANUAL = "manual"
    PROVIDER = "provider_list_price"
    DEFAULT = "default_by_class"
    UNKNOWN = "unknown"


#: Whether a figure derived from this basis may be presented as fact.
EXACT_BASES = frozenset({PriceBasis.MANUAL, PriceBasis.PROVIDER})


@dataclass(frozen=True)
class Price:
    cents: int
    basis: PriceBasis
    currency: str = "EUR"
    #: Which provider stated it, so the UI can name the source; None for a
    #: manual figure or a class default, where no provider was involved.
    provider: str | None = None

    @property
    def is_estimate(self) -> bool:
        return self.basis not in EXACT_BASES


def parse_price_cents(raw: str) -> int | None:
    """Read a price the way it is typed.

    None means the field was left empty, or held only a zero: a deliberate
    "no price", not a parsing failure. ValueError means the text is not a
    price at all.
    """
    text = raw.replace("\u20ac", "").replace("\u00a0", "").replace(" ", "").strip()
    if not text:
        return None
    if "," in text and "." in text:
        # Both separators: the last one is the decimal point (German style),
        # everything before it groups thousands.
        cut = max(text.rfind(","), text.rfind("."))
        text = text[:cut].replace(",", "").replace(".", "") + "." + text[cut + 1 :]
    elif "," in text:
        text = text.replace(",", ".")
    elif "." in text and _THOUSANDS.match(text):
        text = text.replace(".", "")
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"not a price: {raw!r}") from None
    cents = round(value * 100)
    return cents or None


async def resolve_price(
    db: Database,
    settings: Settings,
    media_id: int,
    media_class: str,
    provider_price_cents: int | None = None,
) -> Price:
    """Pick the best price available, and record where it came from.

    A price you entered always wins: you can see the book, and no database
    outranks that. Then the configured price providers in their configured
    order. Then the media-class default, which is a guess and says so.
    """
    manual = await db.fetch_one(
        "SELECT price_cents FROM price_estimates WHERE media_id = ? AND source = 'manual'",
        (media_id,),
    )
    if manual is not None:
        return Price(int(manual["price_cents"]), PriceBasis.MANUAL)

    if provider_price_cents is not None:
        return Price(provider_price_cents, PriceBasis.PROVIDER)

    found = await all_found_prices(db, settings, media_id)
    if found:
        return found[0][1]

    return Price(settings.default_price_cents(media_class), PriceBasis.DEFAULT)


async def all_found_prices(db: Database, settings: Settings, media_id: int) -> list[tuple[str, Price]]:
    """Every provider that stated a price for this work, in configured order.

    Merges catalogue provider records (metadata_records.list_price_*) with
    shop rows stored in price_estimates under their provider name; the
    price_estimates row wins per provider (fresher, and it is where the
    ladder writes). Manual and default sources are excluded — they are not
    what the comparison list is about.
    """
    catalogue = await db.fetch_all(
        "SELECT provider, list_price_cents, list_price_currency FROM metadata_records"
        " WHERE media_id = ? AND status = 'ok' AND list_price_cents IS NOT NULL",
        (media_id,),
    )
    estimates = await db.fetch_all(
        "SELECT source, price_cents, currency FROM price_estimates"
        " WHERE media_id = ? AND source NOT IN ('manual', 'default_by_class', 'provider')",
        (media_id,),
    )

    by_provider: dict[str, Price] = {}
    for row in catalogue:
        name = str(row["provider"])
        by_provider[name] = Price(
            int(row["list_price_cents"]),
            PriceBasis.PROVIDER,
            row["list_price_currency"] or "EUR",
            provider=name,
        )

    # A row stored before the split: it carries no provider name, but the
    # media row remembers which one won, so it can join the comparison list.
    legacy = await db.fetch_one(
        "SELECT price_cents, currency FROM price_estimates WHERE media_id = ? AND source = 'provider'",
        (media_id,),
    )
    if legacy is not None:
        media = await db.fetch_one("SELECT price_provider FROM media WHERE id = ?", (media_id,))
        legacy_name = str(media["price_provider"]) if media and media["price_provider"] else None
        if legacy_name and legacy_name not in by_provider:
            by_provider[legacy_name] = Price(
                int(legacy["price_cents"]), PriceBasis.PROVIDER, legacy["currency"] or "EUR", provider=legacy_name
            )

    for row in estimates:
        name = str(row["source"])
        by_provider[name] = Price(int(row["price_cents"]), PriceBasis.PROVIDER, row["currency"] or "EUR", provider=name)

    order = {name: index for index, name in enumerate(settings.price_providers)}
    ranked = sorted(by_provider.items(), key=lambda item: order.get(item[0], len(order)))
    return [(name, price) for name, price in ranked]


async def _probe_row(db: Database, media_id: int, provider: str, attempted_for: str) -> dict[str, object] | None:
    row = await db.fetch_one(
        "SELECT outcome, checked_at FROM lookup_probes WHERE media_id = ? AND provider = ? AND attempted_for = ?",
        (media_id, provider, attempted_for),
    )
    return dict(row) if row is not None else None


async def _record_probe(
    db: Database, media_id: int, provider: str, attempted_for: str, outcome: str, detail: str | None = None
) -> None:
    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO lookup_probes (media_id, provider, attempted_for, outcome, detail)
            VALUES (:media_id, :provider, :attempted_for, :outcome, :detail)
            ON CONFLICT (media_id, provider, attempted_for) DO UPDATE SET
                outcome = excluded.outcome, detail = excluded.detail,
                checked_at = datetime('now')
            """,
            {
                "media_id": media_id,
                "provider": provider,
                "attempted_for": attempted_for,
                "outcome": outcome,
                "detail": detail,
            },
        )


async def probe_row(db: Database, media_id: int, provider: str, attempted_for: str) -> dict[str, object] | None:
    """Public read of the probe ledger, for the cover ladder and the CLI."""
    return await _probe_row(db, media_id, provider, attempted_for)


async def record_probe(
    db: Database, media_id: int, provider: str, attempted_for: str, outcome: str, detail: str | None = None
) -> None:
    """Public write of the probe ledger."""
    await _record_probe(db, media_id, provider, attempted_for, outcome, detail)


def _probe_is_stale(row: dict[str, object], settings: Settings) -> bool:
    """Only a bot wall or a transport failure is retried; 'not here' is not."""
    if str(row["outcome"]) not in ("blocked", "error"):
        return False
    try:
        checked = datetime.fromisoformat(str(row["checked_at"]))
    except ValueError:
        return False
    return datetime.now(UTC).replace(tzinfo=None) - checked >= timedelta(days=settings.price_retry_days)


async def search_price(
    db: Database,
    settings: Settings,
    client: CachedClient,
    media_id: int,
    query: MediaQuery,
) -> Price | None:
    """Ask every configured price provider that has not been probed yet.

    The first configured provider that answered becomes the effective price;
    the rest are stored for the side-by-side comparison list, so the ladder
    keeps walking after the first hit — the probe table is what keeps the
    extra requests one-time, not an early return.
    """
    from . import PROVIDER_FACTORIES
    from .base import ProviderConfig
    from .matcher import choose
    from .shops import ShopBlocked

    for name in settings.price_providers:
        factory = PROVIDER_FACTORIES.get(name)
        if factory is None:
            continue
        provider = factory(
            ProviderConfig(
                name=name,
                base_url=settings.metadata_base_urls.get(name),
                api_key=settings.provider_api_key(name),
                cookie=settings.provider_cookie(name),
                rate_limit_per_minute=settings.provider_rate_limit(name),
            ),
            client,
        )
        # Catalogue providers are never probed live here; their answers
        # arrive via the enrichment worker's metadata_records and are
        # merged by all_found_prices. Absence from price_providers is
        # configuration, not failure — nothing is written for them.
        if not provider.shop or not provider.available():
            continue

        row = await _probe_row(db, media_id, name, "price")
        if row is not None and not _probe_is_stale(row, settings):
            continue

        try:
            candidates = await provider.search(query)
            best, _needs_confirmation = choose(query, candidates) if candidates else (None, False)
            # A shop price is a price: an unconfirmed match still stands,
            # unlike the enrichment worker's merged bibliographic fields —
            # the manual override always outranks it anyway.
            record = None
            if best is not None:
                record = best.record if best.record is not None else await provider.fetch(best.external_id)
        except ShopBlocked as err:
            await _record_probe(db, media_id, name, "price", "blocked", str(err))
            continue
        except Exception as err:  # a broken shop must not stop the ladder
            _LOGGER.info("price probe %s for media %d failed: %s", name, media_id, err)
            await _record_probe(db, media_id, name, "price", "error", str(err)[:200])
            continue

        if record is None or record.list_price_cents is None:
            await _record_probe(db, media_id, name, "price", "not_found")
            continue

        await _record_probe(db, media_id, name, "price", "found")
        await store_price(
            db,
            media_id,
            Price(
                record.list_price_cents,
                PriceBasis.PROVIDER,
                record.list_price_currency or "EUR",
                provider=name,
            ),
            source=name,
            update_media=False,
        )

    # The effective figure is the configured-order head across everything
    # stored — including catalogue records this loop never asked about. A
    # manual price is untouched: no manual row, no media-row write.
    manual = await db.fetch_one(
        "SELECT 1 FROM price_estimates WHERE media_id = ? AND source = 'manual'",
        (media_id,),
    )
    if manual is not None:
        return None
    found = await all_found_prices(db, settings, media_id)
    if not found:
        return None
    head_name, head = found[0]
    current = await db.fetch_one(
        "SELECT effective_price_cents, price_basis, price_provider FROM media WHERE id = ?",
        (media_id,),
    )
    if (
        current is not None
        and current["effective_price_cents"] == head.cents
        and current["price_basis"] == PriceBasis.PROVIDER.value
        and current["price_provider"] == head_name
    ):
        return head
    await store_price(db, media_id, head, source=head.provider or "provider", price_provider=head_name)
    return head


async def price_search_exhausted(db: Database, settings: Settings, media_id: int) -> bool:
    """True when every configured price source was asked and none produced a price."""
    if not settings.price_providers:
        return False
    priced = await db.fetch_one(
        "SELECT 1 FROM metadata_records WHERE media_id = ? AND status = 'ok' AND list_price_cents IS NOT NULL LIMIT 1",
        (media_id,),
    )
    if priced is not None:
        return False

    # A found probe means some source answered; anything else for every
    # configured source means none did. A catalogue provider counts as asked
    # only when its enrichment job reached a terminal state — being queued is
    # not an answer — and a catalogue price row was excluded above anyway.
    for name in settings.price_providers:
        row = await _probe_row(db, media_id, name, "price")
        if row is not None:
            if str(row["outcome"]) != "found":
                continue
            return False
        job = await db.fetch_one(
            "SELECT state FROM enrichment_jobs WHERE media_id = ? AND provider = ?",
            (media_id, name),
        )
        if job is None or str(job["state"]) in ("queued", "running"):
            return False
    return True


async def search_cover(
    db: Database,
    settings: Settings,
    client: CachedClient,
    media_id: int,
    query: MediaQuery,
) -> None:
    """Walk the configured image providers until a cover is stored.

    The library's own cover URL and the enrichment merge run before this;
    reaching here means both came up empty. Providers already asked stay
    asked (lookup_probes, `attempted_for='cover'`); catalogue answers are
    read from their stored records, shops only once they have a found price
    probe or are freshly asked — no search+fetch per work without cause.
    """
    from . import PROVIDER_FACTORIES
    from .base import ProviderConfig
    from .covers import has_cover, store_cover

    if await has_cover(db, media_id):
        return

    for name in settings.image_providers:
        if name == "library":
            continue  # handled by the caller, from snapshot_items
        if await _probe_row(db, media_id, name, "cover") is not None:
            continue

        factory = PROVIDER_FACTORIES.get(name)
        if factory is None:
            continue
        provider = factory(
            ProviderConfig(
                name=name,
                base_url=settings.metadata_base_urls.get(name),
                api_key=settings.provider_api_key(name),
                cookie=settings.provider_cookie(name),
                rate_limit_per_minute=settings.provider_rate_limit(name),
            ),
            client,
        )
        if not provider.available():
            continue

        url = await _cover_url_for(db, provider, query, media_id)
        if url is None:
            await _record_probe(db, media_id, name, "cover", "not_found")
            continue
        if await store_cover(db, client, media_id, url, provider=name):
            await _record_probe(db, media_id, name, "cover", "found")
            return
        await _record_probe(db, media_id, name, "cover", "not_found", url[:200])


async def _cover_url_for(db: Database, provider: Any, query: MediaQuery, media_id: int) -> str | None:
    """The cover URL one provider offers, from its stored record or live."""
    from .matcher import choose

    stored = await db.fetch_one(
        "SELECT cover_source_url FROM metadata_records WHERE media_id = ? AND provider = ? AND status = 'ok'",
        (media_id, provider.name),
    )
    if stored is not None and stored["cover_source_url"]:
        return str(stored["cover_source_url"])
    if not provider.shop:
        # Catalogue providers are not asked live for a cover alone; the
        # enrichment queue already fetched everything it will.
        return None

    # A shop whose price probe found a record can hand over its cover URL
    # without a second request; one that never answered is not chased.
    price_probe = await _probe_row(db, media_id, provider.name, "price")
    if price_probe is None or str(price_probe["outcome"]) != "found":
        return None

    estimate = await db.fetch_one(
        "SELECT 1 FROM price_estimates WHERE media_id = ? AND source = ?",
        (media_id, provider.name),
    )
    if estimate is None:
        return None
    try:
        candidates = await provider.search(query)
        best, _needs = choose(query, candidates) if candidates else (None, False)
        if best is None:
            return None
        record = best.record if best.record is not None else await provider.fetch(best.external_id)
    except Exception as err:
        _LOGGER.info("cover probe %s for media %d failed: %s", provider.name, media_id, err)
        return None
    return record.cover_source_url if record is not None else None


async def store_price(
    db: Database,
    media_id: int,
    price: Price,
    *,
    source: str,
    note: str | None = None,
    price_provider: str | None = None,
    update_media: bool = True,
) -> None:
    """Store one provider's (or a manual/default) price figure.

    ``update_media=False`` writes only the comparison row: the ladder
    collects every provider's answer this way without letting each of them
    rewrite the effective figure, which the ladder sets once at the end.
    """
    confidence = "exact" if price.basis in EXACT_BASES else "fallback"
    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO price_estimates (media_id, source, price_cents, currency, confidence, note)
            VALUES (:media_id, :source, :cents, :currency, :confidence, :note)
            ON CONFLICT (media_id, source) DO UPDATE SET
                price_cents = excluded.price_cents, currency = excluded.currency,
                confidence = excluded.confidence, note = excluded.note
            """,
            {
                "media_id": media_id,
                "source": source,
                "cents": price.cents,
                "currency": price.currency,
                "confidence": confidence,
                "note": note,
            },
        )
        if update_media:
            await w.execute(
                """
                UPDATE media SET effective_price_cents = ?, price_currency = ?, price_basis = ?,
                                 price_provider = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (price.cents, price.currency, price.basis.value, price_provider, media_id),
            )


async def apply_default_prices(db: Database, settings: Settings) -> int:
    """Give every unpriced work its media-class default, marked as a guess."""
    rows = await db.fetch_all("SELECT id, media_class FROM media WHERE effective_price_cents IS NULL")
    for row in rows:
        price = Price(settings.default_price_cents(row["media_class"]), PriceBasis.DEFAULT)
        await store_price(db, row["id"], price, source="default_by_class", note="Standardpreis")
    return len(rows)


async def clear_price(db: Database, settings: Settings, media_id: int) -> None:
    """Undo a price you typed.

    The next-best figure the providers reported takes its place; with nothing
    left to stand on, the work goes back to unknown rather than re-inheriting
    a class default it had already outgrown.
    """
    async with db.write() as w:
        await w.execute("DELETE FROM price_estimates WHERE media_id = ? AND source = 'manual'", (media_id,))
    found = await all_found_prices(db, settings, media_id)
    if found:
        name, best = found[0]
        # The next-best figure goes back onto the media row only: a
        # catalogue price already lives in metadata_records, and mirroring
        # it into price_estimates here would resurrect it after that record
        # is deleted, which is what the clear-price tests exercise.
        async with db.write() as w:
            await w.execute(
                "UPDATE media SET effective_price_cents = ?, price_currency = ?, "
                "price_basis = 'provider_list_price', price_provider = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (best.cents, best.currency, name, media_id),
            )
        return
    async with db.write() as w:
        await w.execute(
            "UPDATE media SET effective_price_cents = NULL, price_basis = 'unknown', price_provider = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (media_id,),
        )
