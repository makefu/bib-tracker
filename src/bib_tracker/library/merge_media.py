"""Merging two media rows that turn out to be the same work.

Happens when metadata resolves an ISBN that another row already carries: two
catalogue records, one edition each, are one work as far as a reading history
is concerned. A hard merge with an audit row rather than a tombstone, so every
analytic query stays an ordinary join.
"""

from __future__ import annotations

import json
import logging

from ..db.connection import Writer

_LOGGER = logging.getLogger(__name__)


async def find_isbn_owner(w: Writer, isbn: str, exclude_media_id: int) -> int | None:
    row = await w.fetch_one(
        "SELECT id FROM media WHERE isbn13 = ? AND id != ?",
        (isbn, exclude_media_id),
    )
    return int(row["id"]) if row else None


async def merge_media(w: Writer, source_id: int, target_id: int, reason: str = "isbn13") -> int:
    """Fold ``source_id`` into ``target_id``. Returns the loans moved.

    The target keeps whatever it already knows; the source only fills gaps.
    """
    if source_id == target_id:
        return 0

    source = await w.fetch_one("SELECT * FROM media WHERE id = ?", (source_id,))
    target = await w.fetch_one("SELECT * FROM media WHERE id = ?", (target_id,))
    if source is None or target is None:
        return 0

    moved = await w.fetch_one("SELECT COUNT(*) AS n FROM loans WHERE media_id = ?", (source_id,))
    count = int(moved["n"]) if moved else 0

    for table in ("loans", "copies", "fees"):
        await w.execute(f"UPDATE {table} SET media_id = ? WHERE media_id = ?", (target_id, source_id))

    # Ratings and provider records are per work, and the target's win.
    await w.execute("UPDATE OR IGNORE ratings SET media_id = ? WHERE media_id = ?", (target_id, source_id))
    await w.execute("DELETE FROM ratings WHERE media_id = ?", (source_id,))
    await w.execute("UPDATE OR IGNORE metadata_records SET media_id = ? WHERE media_id = ?", (target_id, source_id))
    await w.execute("DELETE FROM metadata_records WHERE media_id = ?", (source_id,))
    await w.execute("UPDATE OR IGNORE price_estimates SET media_id = ? WHERE media_id = ?", (target_id, source_id))
    await w.execute("DELETE FROM price_estimates WHERE media_id = ?", (source_id,))
    await w.execute("DELETE FROM enrichment_jobs WHERE media_id = ?", (source_id,))

    await _fill_gaps(w, source, target)
    await _union_raw_types(w, source, target)

    await w.execute(
        """
        INSERT INTO media_merges (src_media_key, src_title, dst_media_id, reason, loans_moved)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source["media_key"], source["title"], target_id, reason, count),
    )
    await w.execute("DELETE FROM media WHERE id = ?", (source_id,))

    _LOGGER.info("Merged media %d into %d (%s), %d loans moved", source_id, target_id, reason, count)
    return count


async def _fill_gaps(w: Writer, source: object, target: object) -> None:
    """Carry over anything the target does not already know."""
    fields = (
        "author",
        "author_key",
        "isbn13",
        "isbn10",
        "ean",
        "published_year",
        "publisher",
        "page_count",
        "language",
        "description",
        "cover_sha256",
        "cover_aspect",
    )
    updates = {
        field: source[field]  # type: ignore[index]
        for field in fields
        if target[field] in (None, "") and source[field] not in (None, "")  # type: ignore[index]
    }
    if not updates:
        return

    assignments = ", ".join(f"{field} = :{field}" for field in updates)
    await w.execute(
        f"UPDATE media SET {assignments}, updated_at = datetime('now') WHERE id = :id",
        {**updates, "id": target["id"]},  # type: ignore[index]
    )


async def _union_raw_types(w: Writer, source: object, target: object) -> None:
    combined = sorted(
        set(json.loads(source["raw_media_types"] or "[]"))  # type: ignore[index]
        | set(json.loads(target["raw_media_types"] or "[]"))  # type: ignore[index]
    )
    await w.execute(
        "UPDATE media SET raw_media_types = ? WHERE id = ?",
        (json.dumps(combined), target["id"]),  # type: ignore[index]
    )


async def adopt_isbn(w: Writer, media_id: int, isbn: str) -> int:
    """Give a work its ISBN, merging if another row already has it.

    Returns the id the work now lives under, which may not be the one passed in.
    """
    owner = await find_isbn_owner(w, isbn, media_id)
    if owner is not None:
        await merge_media(w, media_id, owner)
        return owner

    await w.execute("UPDATE media SET isbn13 = ?, updated_at = datetime('now') WHERE id = ?", (isbn, media_id))
    return media_id
