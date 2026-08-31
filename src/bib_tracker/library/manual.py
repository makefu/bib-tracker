"""Adding a loan that happened before the tracker saw it.

Written into the observation layer as a synthetic poll run rather than
straight into the derived tables, so a hand-entered loan behaves like every
other one and survives a rebuild.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ..config import Settings
from ..db import queries
from ..db.connection import Database
from .identity import copy_key
from .media_class import MediaClass
from .reconcile import reconcile_pending


@dataclass
class ManualLoan:
    account: str
    title: str
    lend_date: date
    return_date: date | None = None
    author: str | None = None
    isbn: str | None = None
    media_class: MediaClass = MediaClass.BOOK
    publisher: str | None = None


async def add_past_loan(db: Database, settings: Settings, entry: ManualLoan) -> int:
    """Record a loan the tracker never saw. Returns the loan's id."""
    account = await queries.get_account(db, entry.account)
    if account is None:
        raise KeyError(f"Unknown account {entry.account!r}")

    observed_at = datetime.combine(entry.lend_date, datetime.min.time(), tzinfo=UTC)
    raw = _serialised(entry)
    key = copy_key(
        account.library_type,
        title=entry.title,
        author=entry.author,
        media_type=raw["media_type"],
    )

    run_id = await queries.start_run(db, account.id, "import", observed_at)
    await queries.finish_run(
        db,
        run_id,
        status="success",
        finished_at=observed_at,
        duration_ms=0,
        loan_count=1,
        snapshot=[{"copy_key": key, "raw": raw}],
        account_id=account.id,
        observed_at=observed_at,
    )
    await reconcile_pending(db, settings)

    row = await db.fetch_one(
        "SELECT id, loan_key FROM loans WHERE first_seen_run_id = ? ORDER BY id DESC LIMIT 1",
        (run_id,),
    )
    if row is None:  # pragma: no cover - reconciliation just created it
        raise RuntimeError("The imported loan was not reconciled")

    # Both dates were typed, not inferred, so they are recorded as corrections
    # -- which also makes them survive a rebuild.
    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO loan_overrides (loan_key, lend_date, return_date, state, note)
            VALUES (:key, :lend, :ret, :state, 'Von Hand eingetragen')
            ON CONFLICT (loan_key) DO UPDATE SET
                lend_date = excluded.lend_date, return_date = excluded.return_date,
                state = excluded.state, note = excluded.note
            """,
            {
                "key": row["loan_key"],
                "lend": entry.lend_date.isoformat(),
                "ret": entry.return_date.isoformat() if entry.return_date else None,
                "state": "returned" if entry.return_date else "open",
            },
        )

    from .reconcile import apply_overrides

    async with db.write() as w:
        await apply_overrides(w)

    return int(row["id"])


def _serialised(entry: ManualLoan) -> dict[str, Any]:
    """Shaped exactly like a scraped loan, so nothing downstream special-cases it."""
    due = entry.return_date or entry.lend_date
    return {
        "title": entry.title,
        "item_id": f"manual:{entry.isbn or entry.title}",
        "author": entry.author,
        "publisher": entry.publisher,
        "media_type": _MEDIA_TYPE_FOR.get(entry.media_class, "Buch"),
        "due_date": due.isoformat(),
        "checkout_date": entry.lend_date.isoformat(),
        "times_renewed": 0,
        "max_renewals": None,
        "can_be_renewed": False,
        "call_number": None,
        "barcode": None,
        "isbn": entry.isbn,
        "cover_url": None,
        "detail_url": None,
        "library_branch": None,
    }


#: The classifier reads raw library strings, so hand it one it knows.
_MEDIA_TYPE_FOR: dict[MediaClass, str] = {
    MediaClass.BOOK: "Buch",
    MediaClass.AUDIOBOOK: "Hörbuch",
    MediaClass.MUSIC: "CD",
    MediaClass.MOVIE: "DVD",
    MediaClass.GAME: "Konventionelles Spiel",
    MediaClass.MAGAZINE: "Zeitschrift",
    MediaClass.OTHER: "Sonstiges",
}


def json_snapshot(entry: ManualLoan) -> str:
    return json.dumps(_serialised(entry), ensure_ascii=False, sort_keys=True)
