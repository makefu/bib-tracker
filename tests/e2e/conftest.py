"""Browser-driven tests for the served web UI (pytest -m e2e).

These drive a real uvicorn process and a real Chromium against a seeded
database — not the ASGI test client, which executes no JavaScript and so
cannot see the HTMX/Alpine behaviour that is the point of these tests.

The server runs as a separate process (not asgiref's lifespan server, which
needs Django). The environment points BIB_TRACKER_DB_PATH at the seeded
file; the app's lifespan migrates it, which is a no-op on a migrated copy.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page


def _seed(path: Path) -> None:
    """Migrate a fresh file, then insert one controlled loan per sort case.

    The rows are seeded with SQL, not through the poll pipeline: this page is
    about sorting, clearing a rating and the price/rating columns — every one
    of those reads derived/override data the reconciler would also produce,
    and seeding keeps each column's values deliberately distinct.
    """
    from bib_tracker.db.connection import connect
    from bib_tracker.db.migrator import migrate

    conn = connect(path)
    migrate(conn)
    conn.close()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        INSERT INTO accounts (id, name, library_type, username, base_url)
            VALUES (1, 'test', 'koha', '4242', 'http://opac.invalid');
        INSERT INTO poll_runs (id, account_id, trigger, status, started_at, reconciled)
            VALUES (1, 1, 'manual', 'success', '2026-01-01 00:00:00', 1);

        -- title / price / rating / renewals / due / lend / return / status
        -- vary across four works; nothing is alphabetically equal to another.
        INSERT INTO media (id, media_key, media_class, title, author, effective_price_cents,
                           price_basis, first_seen_at, last_seen_at) VALUES
            (1, 'm1', 'book',  'Alpha', 'Ziegler', 100,  'provider_list_price',
             '2026-01-01', '2026-01-01'),
            (2, 'm2', 'book',  'Bravo', 'Yaklich', 500,  'provider_list_price',
             '2026-01-01', '2026-01-01'),
            (3, 'm3', 'book',  'Charlie', 'Xanth', 300,  'provider_list_price',
             '2026-01-01', '2026-01-01'),
            (4, 'm4', 'game',  'Delta', 'Weiss', NULL, 'unknown',
             '2026-01-01', '2026-01-01');

        INSERT INTO copies (id, account_id, copy_key, media_id, library_type,
                            first_seen_at, last_seen_at) VALUES
            (1, 1, 'c1', 1, 'koha', '2026-01-01', '2026-01-01'),
            (2, 1, 'c2', 2, 'koha', '2026-01-01', '2026-01-01'),
            (3, 1, 'c3', 3, 'koha', '2026-01-01', '2026-01-01'),
            (4, 1, 'c4', 4, 'koha', '2026-01-01', '2026-01-01');

        INSERT INTO loans (loan_key, account_id, copy_id, media_id, state,
                           lend_date, lend_date_source, return_date,
                           first_seen_run_id, last_seen_run_id, closing_run_id,
                           first_seen_at, last_seen_at, first_due_date, last_due_date,
                           times_renewed, derived_at) VALUES
            ('l1@1', 1, 1, 1, 'open',     '2026-01-10', 'exact', NULL,
             1, 1, NULL, '2026-01-10', '2026-01-10', '2026-02-09', '2026-02-09',
             1, '2026-01-10'),
            ('l2@1', 1, 2, 2, 'returned', '2026-01-05', 'exact', '2026-02-02',
             1, 1, 1, '2026-01-05', '2026-02-02', '2026-02-02', '2026-02-02',
             0, '2026-02-02'),
            ('l3@1', 1, 3, 3, 'open',     '2026-01-12', 'exact', NULL,
             1, 1, NULL, '2026-01-12', '2026-01-12', '2026-03-09', '2026-03-09',
             0, '2026-01-12'),
            ('l4@1', 1, 4, 4, 'returned', '2026-01-08', 'exact', '2026-01-20',
             1, 1, 1, '2026-01-08', '2026-01-20', '2026-01-22', '2026-01-22',
             3, '2026-01-20');

        -- Delta stays unrated; Alpha and Charlie carry ratings.
        INSERT INTO ratings (media_id, rating) VALUES (1, 3), (3, 5);
        """,
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def app_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real uvicorn process on a free port, serving the seeded database."""
    db_file = tmp_path_factory.mktemp("e2e") / "bib-tracker.db"
    _seed(db_file)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    # The env is what the app itself reads (pydantic-settings, BIB_TRACKER_*);
    # empty config files stop a developer's ~/.config/bib-tracker from leaking
    # into the run.

    env = {
        **os.environ,
        "BIB_TRACKER_DB_PATH": str(db_file),
        "BIB_TRACKER_CONFIG_FILES": "",
        "BIB_TRACKER_METADATA_ENABLED": "false",
        "BIB_TRACKER_POLL_ON_STARTUP": "false",
    }
    log_path = db_file.with_suffix(".log")
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "bib_tracker.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.flush()
                pytest.fail(f"uvicorn exited with {proc.returncode}; server log:\n{log_path.read_text()}")
            try:
                with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("uvicorn did not become ready within 30s")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Chromium's setuid sandbox has no user namespace inside the nix build
    sandbox; the page under test is our own server's, so disabling the OS
    sandbox costs nothing here."""
    return {**browser_type_launch_args, "args": [*browser_type_launch_args.get("args", []), "--no-sandbox"]}


@pytest.fixture
def titles(page: Page):
    """A callable returning the title column, top to bottom — the row order
    the browser actually shows."""

    def read() -> list[str]:
        return page.locator("tbody td .cell-title__name").all_inner_texts()

    return read
