"""What a borrowed item would have cost to buy.

Three sources, in strict order. A price you entered always wins: you can see
the book, and no lookup outranks that. Then a provider's list price. Then a
configured default for the media class, which is a guess and is labelled as
one everywhere it is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..config import Settings
from ..db.connection import Database


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

    @property
    def is_estimate(self) -> bool:
        return self.basis not in EXACT_BASES


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

    stored = await preferred_provider_price(db, settings, media_id)
    if stored is not None:
        return stored

    return Price(settings.default_price_cents(media_class), PriceBasis.DEFAULT)


async def preferred_provider_price(db: Database, settings: Settings, media_id: int) -> Price | None:
    """The best stored provider price, honouring the configured order."""
    rows = await db.fetch_all(
        "SELECT provider, list_price_cents, list_price_currency FROM metadata_records"
        " WHERE media_id = ? AND list_price_cents IS NOT NULL AND status = 'ok'",
        (media_id,),
    )
    by_provider = {row["provider"]: row for row in rows}

    for provider in settings.price_providers:
        row = by_provider.get(provider)
        if row is not None:
            return Price(
                int(row["list_price_cents"]),
                PriceBasis.PROVIDER,
                row["list_price_currency"] or "EUR",
            )

    # A provider that is not in the preference list still beats a pure guess.
    for row in rows:
        return Price(int(row["list_price_cents"]), PriceBasis.PROVIDER, row["list_price_currency"] or "EUR")
    return None


async def store_price(
    db: Database,
    media_id: int,
    price: Price,
    *,
    source: str,
    note: str | None = None,
) -> None:
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
        await w.execute(
            """
            UPDATE media SET effective_price_cents = ?, price_currency = ?, price_basis = ?,
                             updated_at = datetime('now')
            WHERE id = ?
            """,
            (price.cents, price.currency, price.basis.value, media_id),
        )


async def apply_default_prices(db: Database, settings: Settings) -> int:
    """Give every unpriced work its media-class default, marked as a guess."""
    rows = await db.fetch_all("SELECT id, media_class FROM media WHERE effective_price_cents IS NULL")
    for row in rows:
        price = Price(settings.default_price_cents(row["media_class"]), PriceBasis.DEFAULT)
        await store_price(db, row["id"], price, source="default_by_class", note="Standardpreis")
    return len(rows)
