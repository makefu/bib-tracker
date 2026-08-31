"""Statistics over the lending history.

Two rules run through all of it. Durations exclude loans whose start was never
knowable, and say how many were excluded. Money is never a single number: it
always travels with the split between prices that are real and prices that are
a configured guess, because most of them are guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db.connection import Database


@dataclass
class MoneySaved:
    """What borrowing saved, and how much of that is actually known."""

    exact_cents: int = 0
    estimated_cents: int = 0
    exact_count: int = 0
    estimated_count: int = 0
    annual_fees_cents: int = 0

    @property
    def total_cents(self) -> int:
        return self.exact_cents + self.estimated_cents

    @property
    def count(self) -> int:
        return self.exact_count + self.estimated_count

    @property
    def exact_share(self) -> float:
        return self.exact_count / self.count if self.count else 0.0

    @property
    def is_mostly_estimated(self) -> bool:
        return self.exact_share < 0.5

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cents": self.total_cents,
            "exact_cents": self.exact_cents,
            "estimated_cents": self.estimated_cents,
            "basis": {"exact": self.exact_count, "estimated": self.estimated_count},
            "exact_share": round(self.exact_share, 3),
        }


@dataclass
class DurationStats:
    median_days: float | None = None
    mean_days: float | None = None
    p25_days: float | None = None
    p75_days: float | None = None
    counted: int = 0
    #: Loans left out because their start was never observable.
    excluded: int = 0
    by_class: list[dict[str, Any]] = field(default_factory=list)


async def money_saved(db: Database, settings: Settings) -> MoneySaved:
    row = await db.fetch_one(
        """
        SELECT
            SUM(CASE WHEN price_is_exact = 1 THEN price_cents ELSE 0 END) AS exact_cents,
            SUM(CASE WHEN price_is_exact = 0 THEN price_cents ELSE 0 END) AS est_cents,
            SUM(price_is_exact) AS exact_count,
            SUM(1 - price_is_exact) AS est_count
        FROM v_money_saved
        WHERE price_cents IS NOT NULL
        """
    )
    if row is None:
        return MoneySaved()

    return MoneySaved(
        exact_cents=int(row["exact_cents"] or 0),
        estimated_cents=int(row["est_cents"] or 0),
        exact_count=int(row["exact_count"] or 0),
        estimated_count=int(row["est_count"] or 0),
    )


async def durations(db: Database) -> DurationStats:
    rows = await db.fetch_all(
        """
        SELECT days_held, media_class FROM v_loan_durations
        WHERE state = 'returned' AND lend_unreliable = 0 AND days_held IS NOT NULL
        """
    )
    excluded_row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM v_loan_durations WHERE state = 'returned' AND lend_unreliable = 1"
    )
    excluded = int(excluded_row["n"]) if excluded_row else 0

    if not rows:
        return DurationStats(excluded=excluded)

    values = sorted(float(row["days_held"]) for row in rows)
    by_class: dict[str, list[float]] = {}
    for row in rows:
        by_class.setdefault(row["media_class"], []).append(float(row["days_held"]))

    return DurationStats(
        median_days=_quantile(values, 0.5),
        mean_days=sum(values) / len(values),
        p25_days=_quantile(values, 0.25),
        p75_days=_quantile(values, 0.75),
        counted=len(values),
        excluded=excluded,
        by_class=[
            {
                "media_class": media_class,
                "median": _quantile(sorted(items), 0.5),
                "p25": _quantile(sorted(items), 0.25),
                "p75": _quantile(sorted(items), 0.75),
                "count": len(items),
            }
            for media_class, items in sorted(by_class.items(), key=lambda kv: -len(kv[1]))
        ],
    )


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation, matching what a reader expects from a median."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


async def borrowing_pace(db: Database, weeks: int = 26) -> list[dict[str, Any]]:
    """Loans started per calendar week.

    Weekly on purpose: the poll interval makes a day-level series noise, but
    it is irrelevant at this granularity.
    """
    rows = await db.fetch_all(
        """
        SELECT strftime('%Y-%W', eff_lend_date) AS week, COUNT(*) AS n
        FROM v_loan_durations
        WHERE eff_lend_date >= date('now', ?)
        GROUP BY week ORDER BY week
        """,
        (f"-{weeks * 7} day",),
    )
    return [{"week": row["week"], "count": int(row["n"])} for row in rows]


async def media_mix(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT m.media_class, COUNT(*) AS n
        FROM loans l JOIN media m ON m.id = l.media_id
        GROUP BY m.media_class ORDER BY n DESC
        """
    )
    total = sum(int(row["n"]) for row in rows) or 1
    return [{"media_class": row["media_class"], "count": int(row["n"]), "share": int(row["n"]) / total} for row in rows]


async def renewal_behaviour(db: Database) -> dict[str, Any]:
    """How often things get renewed, and what never gets read in time."""
    overall = await db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN times_renewed > 0 THEN 1 ELSE 0 END) AS renewed,
               SUM(CASE WHEN max_renewals > 0 AND times_renewed >= max_renewals THEN 1 ELSE 0 END)
                   AS maxed
        FROM loans
        """
    )
    per_class = await db.fetch_all(
        """
        SELECT m.media_class,
               COUNT(*) AS total,
               SUM(CASE WHEN l.times_renewed > 0 THEN 1 ELSE 0 END) AS renewed
        FROM loans l JOIN media m ON m.id = l.media_id
        GROUP BY m.media_class HAVING total > 0 ORDER BY total DESC
        """
    )
    to_the_limit = await db.fetch_all(
        """
        SELECT m.id AS media_id, m.title, m.media_class, l.times_renewed, l.max_renewals
        FROM loans l JOIN media m ON m.id = l.media_id
        WHERE l.max_renewals > 0 AND l.times_renewed >= l.max_renewals
        ORDER BY l.times_renewed DESC LIMIT 10
        """
    )

    total = int(overall["total"]) if overall and overall["total"] else 0
    renewed = int(overall["renewed"]) if overall and overall["renewed"] else 0
    return {
        "total": total,
        "renewed": renewed,
        "rate": renewed / total if total else 0.0,
        "maxed_out": int(overall["maxed"]) if overall and overall["maxed"] else 0,
        "by_class": [
            {
                "media_class": row["media_class"],
                "total": int(row["total"]),
                "renewed": int(row["renewed"] or 0),
                "rate": int(row["renewed"] or 0) / int(row["total"]),
            }
            for row in per_class
        ],
        "to_the_limit": [dict(row) for row in to_the_limit],
    }


async def overdue_history(db: Database) -> dict[str, Any]:
    """Overdue-ness, from the flag latched when it was observed."""
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(was_overdue) AS overdue,
               SUM(max_overdue_days) AS days,
               MAX(max_overdue_days) AS worst
        FROM loans
        """
    )
    total = int(row["total"]) if row and row["total"] else 0
    overdue = int(row["overdue"]) if row and row["overdue"] else 0
    return {
        "total": total,
        "overdue": overdue,
        "rate": overdue / total if total else 0.0,
        "total_days": int(row["days"]) if row and row["days"] else 0,
        "worst_days": int(row["worst"]) if row and row["worst"] else 0,
    }


async def repeat_borrows(db: Database, minimum: int = 2) -> list[dict[str, Any]]:
    """Works borrowed more than once -- also the "shall we just buy it?" list."""
    rows = await db.fetch_all(
        """
        SELECT m.id AS media_id, m.title, m.author, m.media_class,
               COUNT(*) AS borrows,
               SUM(COALESCE(l.duration_days, 0)) AS total_days,
               m.effective_price_cents, m.price_basis
        FROM loans l JOIN media m ON m.id = l.media_id
        GROUP BY m.id HAVING borrows >= ?
        ORDER BY borrows DESC, total_days DESC LIMIT 20
        """,
        (minimum,),
    )
    return [dict(row) for row in rows]


async def cost_per_day(db: Database, media_class: str = "game", limit: int = 10) -> list[dict[str, Any]]:
    """What a borrowed item worked out at per day it was in the house.

    Days held, not days used: nobody plays a game every evening it sits on the
    shelf, and the figure should not pretend otherwise.
    """
    rows = await db.fetch_all(
        """
        SELECT m.id AS media_id, m.title, m.effective_price_cents, m.price_basis,
               COUNT(*) AS borrows, SUM(COALESCE(l.duration_days, 0)) AS total_days
        FROM loans l JOIN media m ON m.id = l.media_id
        WHERE m.media_class = ? AND m.effective_price_cents IS NOT NULL
        GROUP BY m.id HAVING total_days > 0
        ORDER BY total_days DESC LIMIT ?
        """,
        (media_class, limit),
    )
    return [
        {
            **dict(row),
            "cents_per_day": round(int(row["effective_price_cents"]) / int(row["total_days"])),
        }
        for row in rows
    ]


async def data_quality(db: Database) -> dict[str, Any]:
    """How trustworthy the underlying observations are.

    Shown at the top of the statistics page, because it is what every caveat
    below it rests on.
    """
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN status = 'suspect' THEN 1 ELSE 0 END) AS suspect
        FROM poll_runs WHERE finished_at IS NOT NULL
        """
    )
    inferred = await db.fetch_one(
        """
        SELECT
            SUM(CASE WHEN lend_date_source IN ('exact', 'manual') THEN 1 ELSE 0 END) AS known,
            SUM(CASE WHEN lend_date_source = 'before_tracking' THEN 1 ELSE 0 END) AS unknown,
            COUNT(*) AS total
        FROM loans
        """
    )
    total = int(row["total"]) if row and row["total"] else 0
    ok = int(row["ok"]) if row and row["ok"] else 0
    return {
        "runs": total,
        "successful": ok,
        "success_rate": ok / total if total else 0.0,
        "suspect": int(row["suspect"]) if row and row["suspect"] else 0,
        "loans": int(inferred["total"]) if inferred and inferred["total"] else 0,
        "dates_known": int(inferred["known"]) if inferred and inferred["known"] else 0,
        "dates_unknown": int(inferred["unknown"]) if inferred and inferred["unknown"] else 0,
    }


async def overview(db: Database, settings: Settings) -> dict[str, Any]:
    """Everything the statistics page shows, in one call."""
    return {
        "money": (await money_saved(db, settings)).as_dict(),
        "durations": await durations(db),
        "pace": await borrowing_pace(db),
        "media_mix": await media_mix(db),
        "renewals": await renewal_behaviour(db),
        "overdue": await overdue_history(db),
        "repeats": await repeat_borrows(db),
        "cost_per_day": await cost_per_day(db),
        "quality": await data_quality(db),
    }
