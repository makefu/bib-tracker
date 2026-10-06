"""Operator-triggered maintenance and the settings page that drives it.

The runner is the only way backfills happen after a feature ships, so the
rules that matter here are: one task at a time, a failed task stays visible,
destructive tasks respect the observation layer (manual prices survive), and
vacuum releases space instead of only reorganising it.
"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from bib_tracker.maintenance import KIND_LABELS, MaintenanceBusy, MaintenanceRunner


async def _media(db, rows: int) -> list[int]:
    ids: list[int] = []
    async with db.write() as w:
        for n in range(rows):
            ids.append(
                int(
                    await w.execute(
                        "INSERT INTO media (media_key, media_class, title, first_seen_at, last_seen_at)"
                        " VALUES (?, 'book', ?, datetime('now'), datetime('now'))",
                        (f"m{n}", f"Werk {n}"),
                    )
                )
            )
    return ids


class FakeWorker:
    """Records ladder calls; gate/fail drive the busy and failure cases."""

    def __init__(self) -> None:
        self.ladders: list[int] = []
        self.run_once_calls = 0
        self.gate: asyncio.Event | None = None
        self.fail: str | None = None

    async def run_ladders(self, media_id: int) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise RuntimeError(self.fail)
        self.ladders.append(media_id)

    async def run_once(self, limit: int = 20) -> int:
        del limit
        self.run_once_calls += 1
        return 0 if self.run_once_calls > 1 else 2


async def _drift(runner: MaintenanceRunner) -> None:
    """A task runs detached; the card polls, so tests poll the same way."""
    for _ in range(300):
        if runner.status().finished_at is not None:
            return
        await asyncio.sleep(0.01)


# -- runner ----------------------------------------------------------------


async def test_a_finished_task_is_reported_with_its_progress(db, settings) -> None:
    ids = await _media(db, 3)
    worker = FakeWorker()
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]

    runner.start("price", scope="all")
    await _drift(runner)

    status = runner.status()
    assert status.kind == "price"
    assert status.label == KIND_LABELS["price"]
    assert (status.total, status.done) == (3, 3)
    assert status.running is False
    assert worker.ladders == ids


async def test_a_second_task_is_refused_while_one_runs(db, settings) -> None:
    await _media(db, 1)
    worker = FakeWorker()
    worker.gate = asyncio.Event()
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]

    runner.start("price", scope="all")
    with pytest.raises(MaintenanceBusy):
        runner.start("cover", scope="all")
    worker.gate.set()


async def test_a_failed_task_stays_visible_instead_of_vanishing(db, settings) -> None:
    await _media(db, 1)
    worker = FakeWorker()
    worker.fail = "Thalia: HTTP 403"
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]

    runner.start("price", scope="all")
    await _drift(runner)

    status = runner.status()
    assert status.error == "Thalia: HTTP 403"
    assert status.running is False
    # Not busy any more: the operator can retry.
    runner.start("price", scope="all")


async def test_a_workerless_runner_refuses_the_network_tasks(db, settings) -> None:
    await _media(db, 1)
    runner = MaintenanceRunner(db, settings, None)

    for kind in ("price", "cover"):
        runner.start(kind, scope="all")
        await _drift(runner)
        assert "disabled" in (runner.status().error or "")


async def test_fresh_forgets_only_the_probes_of_that_purpose(db, settings) -> None:
    (media_id,) = await _media(db, 1)
    async with db.write() as w:
        for provider, purpose in (("buch7", "price"), ("thalia", "cover"), ("vlb", "price")):
            await w.execute(
                "INSERT INTO lookup_probes (media_id, provider, attempted_for, outcome) VALUES (?, ?, ?, 'not_found')",
                (media_id, provider, purpose),
            )

    worker = FakeWorker()
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]
    runner.start("price", fresh=True, scope="all")
    await _drift(runner)

    rows = await db.fetch_all("SELECT provider, attempted_for FROM lookup_probes")
    assert [row["attempted_for"] for row in rows] == ["cover"]
    assert rows[0]["provider"] == "thalia"


async def test_the_missing_scope_skips_works_that_already_have_a_price(db, settings) -> None:
    ids = await _media(db, 2)
    async with db.write() as w:
        await w.execute(
            "UPDATE media SET effective_price_cents = 999, price_basis = 'provider_list_price',"
            " price_provider = 'buch7' WHERE id = ?",
            (ids[0],),
        )

    worker = FakeWorker()
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]
    runner.start("price", scope="missing")
    await _drift(runner)

    assert worker.ladders == [ids[1]]


async def test_reenrich_keeps_manual_prices_but_forgets_the_provider_layer(db, settings) -> None:
    (media_id,) = await _media(db, 1)
    async with db.write() as w:
        await w.execute(
            "INSERT INTO metadata_records (media_id, provider, status, list_price_cents,"
            " list_price_currency, fetched_at) VALUES (?, 'vlb', 'ok', 1400, 'EUR', datetime('now'))",
            (media_id,),
        )
        await w.execute(
            "INSERT INTO price_estimates (media_id, source, price_cents, confidence)"
            " VALUES (?, 'manual', 999, 'exact')",
            (media_id,),
        )
        await w.execute(
            "INSERT INTO lookup_probes (media_id, provider, attempted_for, outcome)"
            " VALUES (?, 'vlb', 'price', 'found')",
            (media_id,),
        )

    worker = FakeWorker()
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]
    runner.start("reenrich")
    await _drift(runner)

    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM metadata_records"))["n"] == 0
    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM lookup_probes"))["n"] == 0
    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM price_estimates"))["n"] == 1
    row = await db.fetch_one("SELECT metadata_state FROM media WHERE id = ?", (media_id,))
    assert row["metadata_state"] == "pending"
    assert worker.run_once_calls >= 1


async def test_covers_reload_clears_the_pointers_and_the_rows(db, settings) -> None:
    """media.cover_sha256 references images.sha256 informally; a reload must
    leave no row pointing at a hash that is no longer served."""
    (media_id,) = await _media(db, 1)
    async with db.write() as w:
        await w.execute(
            "INSERT INTO images (sha256, variant, mime, byte_size, fetched_at, data)"
            " VALUES ('aa', 'md', 'image/webp', 10, datetime('now'), x'00')"
        )
        await w.execute("UPDATE media SET cover_sha256 = 'aa' WHERE id = ?", (media_id,))

    worker = FakeWorker()
    runner = MaintenanceRunner(db, settings, worker)  # type: ignore[arg-type]
    runner.start("covers-reload")
    await _drift(runner)

    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM images"))["n"] == 0
    row = await db.fetch_one("SELECT cover_sha256, cover_aspect FROM media WHERE id = ?", (media_id,))
    assert row["cover_sha256"] is None
    assert worker.ladders == [media_id]


def test_vacuum_returns_space(db) -> None:
    """sqlite keeps freed pages in the file; VACUUM must hand them back or the
    settings button is a placebo."""
    conn = sqlite3.connect(db.path)
    conn.execute("CREATE TABLE IF NOT EXISTS pad (b BLOB)")
    conn.execute("INSERT INTO pad SELECT randomblob(4000) FROM sqlite_master LIMIT 3000")
    conn.commit()
    conn.execute("DELETE FROM pad")
    conn.commit()
    before = conn.execute("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()").fetchone()[0]
    conn.close()

    MaintenanceRunner(db, None, None)._vacuum_sync()  # type: ignore[arg-type]

    conn = sqlite3.connect(db.path)
    after = conn.execute("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()").fetchone()[0]
    conn.close()
    assert int(after) < int(before)


# -- settings page ----------------------------------------------------------


async def test_the_settings_page_renders_with_an_empty_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/settings")
    assert response.status_code == 200
    body = response.text
    assert "Einstellungen" in body
    assert 'id="maintenance-card"' in body


async def test_the_settings_page_names_the_provider_config_the_ladders_use(
    client: httpx.AsyncClient,
) -> None:
    """The card lists every configured provider by display name, so an
    operator can see which sources a price is asked of."""
    body = (await client.get("/settings")).text
    for label in ("Thalia", "Buchkatalog.de", "buch7.de"):
        assert label in body


async def test_the_settings_page_counts_live(db, client: httpx.AsyncClient) -> None:
    """Completeness is counted per request, so a work added after the last
    maintenance run shows up at once."""
    await _media(db, 2)
    body = (await client.get("/settings")).text
    assert ">2<" in body


async def test_the_card_reports_an_unusable_provider(client: httpx.AsyncClient, settings) -> None:
    """A provider whose credential is missing must be marked, not listed as
    if it could answer."""
    settings.metadata_api_keys = {**settings.metadata_api_keys, "bgg": ""}
    settings.price_providers = [*settings.price_providers, "bgg"]
    body = (await client.get("/api/maintenance/card")).text
    item = next(part for part in body.split("provider-list__item") if "BoardGameGeek" in part)
    assert 'data-tone="warn"' in item.split("</span>")[0]
    assert "braucht Zugangsdaten" in item


# -- API ---------------------------------------------------------------------


async def test_the_card_is_the_post_response(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/maintenance/rebuild-history")
    assert response.status_code == 200
    assert 'id="maintenance-card"' in response.text


async def test_an_unknown_task_is_not_started(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/maintenance/drop-tables")).status_code == 404


async def test_vacuum_accepts_the_cache_checkbox(client: httpx.AsyncClient, db) -> None:
    async with db.write() as w:
        await w.execute(
            "INSERT INTO http_cache (url_hash, url, status, body, fetched_at)"
            " VALUES ('h', 'http://x.test/a', 200, x'00', datetime('now'))"
        )
    response = await client.post("/api/maintenance/vacuum", data={"clear_cache": "1"})
    assert response.status_code == 200
    assert 'id="maintenance-card"' in response.text

    # The task runs detached; poll the status endpoint the card polls.
    for _ in range(300):
        if not (await client.get("/api/maintenance/status")).json()["running"]:
            break
        await asyncio.sleep(0.01)
    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM http_cache"))["n"] == 0


async def test_a_second_start_while_busy_conflicts(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The task runs detached; while it is in flight a second start is a
    conflict, not a second hammering of the same providers."""
    from bib_tracker.library import reconcile

    class Report:
        opened = closed = reopened = renewed = updated = 0

    async def slow_rebuild(db, settings):
        del db, settings
        await asyncio.sleep(0.3)
        return Report()

    monkeypatch.setattr(reconcile, "rebuild_history", slow_rebuild)
    started = await client.post("/api/maintenance/rebuild-history")
    assert started.status_code == 200
    conflict = await client.post("/api/maintenance/rebuild-history")
    assert conflict.status_code == 409
    for _ in range(300):
        if not (await client.get("/api/maintenance/status")).json()["running"]:
            break
        await asyncio.sleep(0.01)


# -- per-media re-crawl -------------------------------------------------------


async def test_recrawl_buttons_are_disabled_without_enrichment(client: httpx.AsyncClient, db) -> None:
    (media_id,) = await _media(db, 1)
    body = (await client.get(f"/media/{media_id}")).text
    assert "Preis neu suchen" in body
    assert "Cover neu laden" in body
    # The client fixture runs with metadata_enabled=False.
    button = next(part for part in body.split("<button") if "Preis neu suchen" in part)
    assert "disabled" in button


async def test_recrawling_a_missing_work_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/media/999/crawl", data={"what": "price"})
    assert response.status_code == 404


async def test_recrawl_rejects_an_unknown_what(client: httpx.AsyncClient, db) -> None:
    (media_id,) = await _media(db, 1)
    response = await client.post(f"/api/media/{media_id}/crawl", data={"what": "rating"})
    assert response.status_code == 400


async def test_the_probe_ledger_is_visible_per_work(client: httpx.AsyncClient, db) -> None:
    (media_id,) = await _media(db, 1)
    async with db.write() as w:
        await w.execute(
            "INSERT INTO lookup_probes (media_id, provider, attempted_for, outcome)"
            " VALUES (?, 'thalia', 'price', 'blocked')",
            (media_id,),
        )
    body = (await client.get(f"/media/{media_id}")).text
    assert "Recherche" in body
    assert "zugehalten" in body
