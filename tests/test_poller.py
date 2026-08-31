"""Polling, driven through the real backends against real recorded markup.

Deliberately not a mocked backend: these tests double as the regression net
against a future ha_stadtbibliothek release changing what it parses out.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bib_tracker.config import AccountConfig
from bib_tracker.library.poller import PollStatus, poll_account

UPSTREAM_FIXTURES = Path(__file__).parent / "fixtures" / "library"
BASE_URL = "http://opac.test"


def _fixture(name: str) -> str:
    return (UPSTREAM_FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def account() -> AccountConfig:
    return AccountConfig(
        name="remseck",
        library_type="remseck",
        username="12345",
        base_url=BASE_URL,
    )


def _mock_remseck(checkouts: str, *, account_page: str | None = None) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=account_page or checkouts)
    )
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=_fixture("remseck_no_fees.html"))
    )


@respx.mock
async def test_successful_poll_returns_the_loans(account: AccountConfig) -> None:
    _mock_remseck(_fixture("remseck_checkouts.html"))

    result = await poll_account(account, "hunter2")

    assert result.status is PollStatus.SUCCESS
    assert result.ok
    titles = [loan.title for loan in result.loans]
    assert titles == ["Die unendliche Geschichte", "Momo", "Tschick", "Krabat"]
    assert result.duration_ms >= 0
    assert result.error_message is None


@respx.mock
async def test_successful_poll_serialises_loans_for_storage(account: AccountConfig) -> None:
    _mock_remseck(_fixture("remseck_checkouts.html"))

    result = await poll_account(account, "hunter2")
    first = result.serialised_loans()[0]

    assert first["title"] == "Die unendliche Geschichte"
    assert first["author"] == "Ende, Michael"
    assert first["detail_url"].endswith("biblionumber=12345")
    assert isinstance(first["due_date"], str)


@respx.mock
async def test_bad_credentials_are_reported_as_an_auth_error(account: AccountConfig) -> None:
    """The single most important distinction in the whole application: this
    must never be recorded as "the account is empty"."""
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=_fixture("remseck_login.html"))
    )

    result = await poll_account(account, "wrong")

    assert result.status is PollStatus.AUTH_ERROR
    assert not result.ok
    assert result.loans == []
    assert result.error_kind == "AuthenticationError"


@respx.mock
async def test_an_expired_session_is_a_parse_error_not_an_empty_account(account: AccountConfig) -> None:
    _mock_remseck(_fixture("remseck_login.html"), account_page=_fixture("remseck_checkouts.html"))

    result = await poll_account(account, "hunter2")

    assert result.status is PollStatus.PARSE_ERROR
    assert result.loans == []


@respx.mock
async def test_an_empty_account_is_a_success_with_no_loans(account: AccountConfig) -> None:
    _mock_remseck(_fixture("remseck_no_checkouts.html"))

    result = await poll_account(account, "hunter2")

    assert result.status is PollStatus.SUCCESS
    assert result.loans == []


@respx.mock
async def test_a_connection_failure_is_a_network_error(account: AccountConfig) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(side_effect=httpx.ConnectError("no route"))

    result = await poll_account(account, "hunter2")

    assert result.status is PollStatus.NETWORK_ERROR
    assert result.error_kind == "ConnectError"


async def test_an_unknown_library_type_is_an_internal_error() -> None:
    account = AccountConfig(name="x", library_type="atlantis", username="1")

    result = await poll_account(account, "hunter2")

    assert result.status is PollStatus.INTERNAL_ERROR


@respx.mock
async def test_stuttgart_fees_are_reported_as_unsupported() -> None:
    """Stuttgart returns no fees because they are not implemented, which must
    not be written to the history as "you owe nothing"."""
    account = AccountConfig(name="stuttgart", library_type="stuttgart", username="1", base_url=BASE_URL)
    respx.get(f"{BASE_URL}?service=direct/0/Home/$DirectLink&sp=SOPAC").mock(
        return_value=httpx.Response(200, text=_fixture("stuttgart_home.html"))
    )
    respx.post(url__regex=r".*jsessionid=TEST.*").mock(
        side_effect=[
            httpx.Response(200, text=_fixture("stuttgart_login_form.html")),
            httpx.Response(200, text=_fixture("stuttgart_logged_in.html")),
        ]
    )
    respx.get(url__regex=r".*SBK00000001.*").mock(
        return_value=httpx.Response(200, text=_fixture("stuttgart_ausleihen.html"))
    )

    result = await poll_account(account, "hunter2")

    assert result.status is PollStatus.SUCCESS
    assert result.fees_supported is False
    assert result.fees == []


@respx.mock
async def test_stuttgart_media_rows_carry_call_number_and_publisher() -> None:
    """Guards the upstream parser fix we depend on for board-game identity."""
    account = AccountConfig(name="stuttgart", library_type="stuttgart", username="1", base_url=BASE_URL)
    respx.get(f"{BASE_URL}?service=direct/0/Home/$DirectLink&sp=SOPAC").mock(
        return_value=httpx.Response(200, text=_fixture("stuttgart_home.html"))
    )
    respx.post(url__regex=r".*jsessionid=TEST.*").mock(
        side_effect=[
            httpx.Response(200, text=_fixture("stuttgart_login_form.html")),
            httpx.Response(200, text=_fixture("stuttgart_logged_in.html")),
        ]
    )
    respx.get(url__regex=r".*SBK00000001.*").mock(
        return_value=httpx.Response(200, text=_fixture("stuttgart_ausleihen.html"))
    )

    result = await poll_account(account, "hunter2")
    game = next(loan for loan in result.loans if loan.media_type == "Konventionelles Spiel")

    assert game.call_number == "S-SPIEL CAT"
    assert game.publisher == "Kosmos"
    assert game.author is None
    assert game.barcode is None
