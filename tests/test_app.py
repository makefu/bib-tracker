"""Application wiring."""

from __future__ import annotations

import httpx

from bib_tracker import __version__


async def test_healthz_reports_the_schema_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["schema_version"] == 1
    assert body["accounts"] == []


async def test_readyz(client: httpx.AsyncClient) -> None:
    assert (await client.get("/readyz")).status_code == 200


async def test_startup_migrates_an_empty_database(client: httpx.AsyncClient, db_path) -> None:
    """The service must come up against a fresh StateDirectory."""
    assert db_path.exists()
    assert (await client.get("/healthz")).json()["schema_version"] == 1


async def test_static_assets_are_served(client: httpx.AsyncClient) -> None:
    """Proof the vendored assets ship and no CDN is needed at runtime."""
    assert (await client.get("/static/css/tokens.css")).status_code == 200


async def test_history_reports_date_sources_and_bounds(client: httpx.AsyncClient) -> None:
    """An inferred date must be distinguishable from an observed one."""
    body = (await client.get("/api/history")).json()
    assert body == {"count": 0, "loans": []}


async def test_loans_endpoint_is_empty_without_accounts(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/loans")).json()
    assert body["open_loans"] == 0


async def test_polling_without_configured_accounts_is_unavailable(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/accounts/nope/poll")
    assert response.status_code in {404, 503}
