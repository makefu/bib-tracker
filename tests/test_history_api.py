"""The history endpoint, against a database built by real polls."""

from __future__ import annotations

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager

from bib_tracker.app import create_app
from tests.conftest import library_fixture

BASE_URL = "http://opac.test"


@pytest.fixture
async def api(settings, account_config, tmp_path):
    """An app whose single account points at the mocked OPAC."""
    import json

    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps(
            [
                {
                    "name": account_config.name,
                    "library_type": account_config.library_type,
                    "username": account_config.username,
                    "base_url": account_config.base_url,
                    "password_file": str(account_config.password_file),
                }
            ]
        )
    )
    settings.accounts_file = accounts_file
    settings.poll_on_startup = False

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _mock(checkouts: str) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )


@respx.mock
async def test_history_lists_current_and_returned_loans(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    _mock(library_fixture("remseck_checkouts_returned.html"))
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/api/history")).json()
    assert body["count"] == 4
    by_title = {loan["title"]: loan for loan in body["loans"]}

    returned = by_title["Die unendliche Geschichte"]
    assert returned["state"] == "returned"
    assert returned["return_date"] is not None
    assert returned["return_date_source"] == "last_seen"
    assert returned["account"] == "remseck"

    still_out = by_title["Momo"]
    assert still_out["state"] == "open"
    assert still_out["return_date"] is None


@respx.mock
async def test_history_can_be_filtered_by_state(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    _mock(library_fixture("remseck_checkouts_returned.html"))
    await api.post("/api/accounts/remseck/poll")

    open_body = (await api.get("/api/history", params={"state": "open"})).json()
    assert {loan["state"] for loan in open_body["loans"]} == {"open"}
    assert open_body["count"] == 3


@respx.mock
async def test_a_manual_correction_overrides_the_inferred_date(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    loans = (await api.get("/api/history")).json()["loans"]
    target = loans[0]
    assert target["lend_date_source"] != "manual"

    response = await api.post(
        f"/api/loans/{target['loan_key']}/override",
        json={"lend_date": "2026-01-05", "note": "counter receipt"},
    )
    assert response.status_code == 200

    corrected = {loan["loan_key"]: loan for loan in (await api.get("/api/history")).json()["loans"]}
    assert corrected[target["loan_key"]]["lend_date"] == "2026-01-05"
    assert corrected[target["loan_key"]]["lend_date_source"] == "manual"


@respx.mock
async def test_a_second_poll_of_one_account_is_refused_while_running(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    response = await api.post("/api/accounts/remseck/poll")
    assert response.status_code == 202
    assert "run_id" in response.json()
