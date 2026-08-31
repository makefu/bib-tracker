"""Folding two catalogue records into one work."""

from __future__ import annotations

from bib_tracker.db.connection import Database
from bib_tracker.library.merge_media import adopt_isbn, merge_media


async def _media(db: Database, key: str, title: str, **extra) -> int:
    columns = {
        "media_key": key,
        "media_class": "book",
        "title": title,
        "first_seen_at": "2026-01-01T00:00:00",
        "last_seen_at": "2026-01-01T00:00:00",
        **extra,
    }
    names = ", ".join(columns)
    placeholders = ", ".join(f":{name}" for name in columns)
    async with db.write() as w:
        return await w.execute(f"INSERT INTO media ({names}) VALUES ({placeholders})", columns)


async def _loan(db: Database, media_id: int, loan_key: str) -> None:
    async with db.write() as w:
        account_id = await w.execute(
            "INSERT INTO accounts (name, library_type, username) VALUES (?, 'remseck', ?)",
            (f"acct-{loan_key}", f"user-{loan_key}"),
        )
        run_id = await w.execute(
            "INSERT INTO poll_runs (account_id, trigger, status, started_at)"
            " VALUES (?, 'manual', 'success', datetime('now'))",
            (account_id,),
        )
        copy_id = await w.execute(
            """
            INSERT INTO copies (account_id, copy_key, media_id, library_type, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 'remseck', datetime('now'), datetime('now'))
            """,
            (account_id, f"copy-{loan_key}", media_id),
        )
        await w.execute(
            """
            INSERT INTO loans (
                loan_key, account_id, copy_id, media_id, state, lend_date, lend_date_source,
                first_seen_run_id, last_seen_run_id, first_seen_at, last_seen_at,
                first_due_date, last_due_date, derived_at
            ) VALUES (?, ?, ?, ?, 'open', '2026-01-01', 'first_seen', ?, ?, datetime('now'),
                      datetime('now'), '2026-02-01', '2026-02-01', datetime('now'))
            """,
            (loan_key, account_id, copy_id, media_id, run_id, run_id),
        )


async def test_two_records_with_one_isbn_become_one_work(db: Database) -> None:
    """Two editions in the catalogue are one book in a reading history."""
    keeper = await _media(db, "a", "Die unendliche Geschichte", isbn13="9783522621885")
    duplicate = await _media(db, "b", "Unendliche Geschichte, Die")
    await _loan(db, keeper, "l1")
    await _loan(db, duplicate, "l2")

    async with db.write() as w:
        surviving = await adopt_isbn(w, duplicate, "9783522621885")

    assert surviving == keeper
    remaining = await db.fetch_all("SELECT id FROM media")
    assert [row["id"] for row in remaining] == [keeper]

    loans = await db.fetch_all("SELECT loan_key, media_id FROM loans ORDER BY loan_key")
    assert {row["media_id"] for row in loans} == {keeper}
    assert len(loans) == 2


async def test_a_free_isbn_is_simply_recorded(db: Database) -> None:
    media_id = await _media(db, "a", "Momo")

    async with db.write() as w:
        surviving = await adopt_isbn(w, media_id, "9783522621885")

    assert surviving == media_id
    row = await db.fetch_one("SELECT isbn13 FROM media WHERE id = ?", (media_id,))
    assert row["isbn13"] == "9783522621885"


async def test_the_merge_fills_gaps_but_does_not_overwrite(db: Database) -> None:
    keeper = await _media(db, "a", "Momo", author="Ende, Michael", isbn13="978")
    source = await _media(db, "b", "Momo", author="Falscher Name", publisher="Thienemann")

    async with db.write() as w:
        await merge_media(w, source, keeper)

    row = await db.fetch_one("SELECT author, publisher FROM media WHERE id = ?", (keeper,))
    assert row["author"] == "Ende, Michael"
    assert row["publisher"] == "Thienemann"


async def test_the_merge_is_recorded_for_inspection(db: Database) -> None:
    keeper = await _media(db, "a", "Momo", isbn13="978")
    source = await _media(db, "b", "Momo (Neuausgabe)")
    await _loan(db, source, "l1")

    async with db.write() as w:
        moved = await merge_media(w, source, keeper)

    assert moved == 1
    audit = await db.fetch_one("SELECT * FROM media_merges")
    assert audit["src_title"] == "Momo (Neuausgabe)"
    assert audit["dst_media_id"] == keeper
    assert audit["loans_moved"] == 1
    assert audit["reason"] == "isbn13"


async def test_your_rating_survives_a_merge(db: Database) -> None:
    keeper = await _media(db, "a", "Momo", isbn13="978")
    source = await _media(db, "b", "Momo (Neuausgabe)")
    async with db.write() as w:
        await w.execute("INSERT INTO ratings (media_id, rating) VALUES (?, 4)", (source,))
        await merge_media(w, source, keeper)

    row = await db.fetch_one("SELECT rating FROM ratings WHERE media_id = ?", (keeper,))
    assert row is not None
    assert row["rating"] == 4


async def test_a_rating_on_the_survivor_is_not_replaced(db: Database) -> None:
    keeper = await _media(db, "a", "Momo", isbn13="978")
    source = await _media(db, "b", "Momo (Neuausgabe)")
    async with db.write() as w:
        await w.execute("INSERT INTO ratings (media_id, rating) VALUES (?, 5)", (keeper,))
        await w.execute("INSERT INTO ratings (media_id, rating) VALUES (?, 2)", (source,))
        await merge_media(w, source, keeper)

    rows = await db.fetch_all("SELECT media_id, rating FROM ratings")
    assert len(rows) == 1
    assert rows[0]["rating"] == 5


async def test_merging_a_row_into_itself_does_nothing(db: Database) -> None:
    media_id = await _media(db, "a", "Momo")

    async with db.write() as w:
        assert await merge_media(w, media_id, media_id) == 0

    assert len(await db.fetch_all("SELECT id FROM media")) == 1
