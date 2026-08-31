"""Drive the poller with the pages the real OPACs actually serve.

The fixtures in `tests/fixtures/library/` are constructed: four loans, two of
them returned in the next snapshot, which is what the reconciliation tests
need and what no live account will hold still for. `tests/fixtures/library/
recorded/` is the other half of the picture -- verbatim captures from both
installations, copied from ha_stadtbibliothek, where they are recorded and
anonymised.

They are here because the failure this project is most exposed to is a poll
that succeeds with the wrong answer. A parse that quietly yields nothing is
recorded as an account with nothing on loan, and the reconciler then reports
every book returned on the same day. That has happened, and it happened
because a fixture agreed with the parser and neither agreed with the library.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bib_tracker.library.identity import copy_key, media_key
from bib_tracker.library.media_class import MediaClass, classify
from bib_tracker.library.poller import PollStatus, poll_account
from tests.conftest import recorded_fixture

REMSECK = "http://opac.test"
STUTTGART = "http://adis.test"


@pytest.fixture
def stuttgart_config(tmp_path):
    from bib_tracker.config import AccountConfig

    password_file = tmp_path / "password"
    password_file.write_text("hunter2\n")
    return AccountConfig(
        name="stuttgart",
        library_type="stuttgart",
        username="12345",
        base_url=STUTTGART,
        password_file=password_file,
    )


def _mock_remseck(checkouts: str, *, login: str | None = None, account: str = "remseck_account.html") -> None:
    """Serve the account pages. `login` defaults to the same page the loan
    fetch gets; pass it separately to make the session die after login."""
    respx.post(f"{REMSECK}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=recorded_fixture(login or checkouts))
    )
    respx.get(f"{REMSECK}/cgi-bin/koha/opac-user.pl").mock(
        return_value=httpx.Response(200, html=recorded_fixture(checkouts))
    )
    respx.get(f"{REMSECK}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=recorded_fixture(account))
    )


def _mock_stuttgart(ausleihen: str = "stuttgart_ausleihen.html") -> None:
    """Serve the four pages the aDIS login walks through.

    The flow is GET the search mask, POST it to reach the credentials form,
    POST those to reach the account overview, then follow its Ausleihen link.
    Every step reads the next URL out of the page before it, so mocking by
    path alone is enough.
    """
    respx.get(url__startswith=f"{STUTTGART}/?service=").mock(
        return_value=httpx.Response(200, html=recorded_fixture("stuttgart_home.html"))
    )
    posts = iter(
        [
            recorded_fixture("stuttgart_login_form.html"),
            recorded_fixture("stuttgart_account.html"),
        ]
    )
    respx.post(url__startswith=f"{STUTTGART}/aDISWeb/app").mock(
        side_effect=lambda request: httpx.Response(200, html=next(posts))
    )
    respx.get(url__startswith=f"{STUTTGART}/aDISWeb/app").mock(
        return_value=httpx.Response(200, html=recorded_fixture(ausleihen))
    )


# --- the poll itself -----------------------------------------------------


@respx.mock
async def test_a_recorded_koha_account_polls_successfully(account_config) -> None:
    _mock_remseck("remseck_checkouts.html")
    result = await poll_account(account_config, "hunter2")

    assert result.status is PollStatus.SUCCESS
    assert len(result.loans) == 27
    assert result.fees == []
    assert result.fees_supported is True
    # Everything the database writes has to survive serialisation.
    assert len(result.serialised_loans()) == 27
    assert all(row["title"] and row["due_date"] for row in result.serialised_loans())


@respx.mock
async def test_an_expired_session_is_a_parse_error_not_an_emptied_account(account_config) -> None:
    """The failure mode this whole project has to be proof against.

    A logged-out page has no loan table on it. Reading that as "nothing is
    borrowed" would have the reconciler close every open loan at once and
    report the lot as returned today -- a fabricated lending history that
    looks entirely plausible afterwards.
    """
    _mock_remseck("remseck_logged_out.html", login="remseck_checkouts.html")
    result = await poll_account(account_config, "hunter2")

    assert result.status is PollStatus.PARSE_ERROR
    assert result.loans == []
    assert result.error_kind == "ParseError"


@respx.mock
async def test_a_rejected_login_is_an_auth_error(account_config) -> None:
    """Koha answers bad credentials with the login page, which is the same
    page an expired session gets. Only the point in the poll where it arrives
    separates the two, and they must not be recorded as the same failure."""
    _mock_remseck("remseck_logged_out.html")
    result = await poll_account(account_config, "hunter2")

    assert result.status is PollStatus.AUTH_ERROR
    assert result.error_kind == "AuthenticationError"


@respx.mock
async def test_a_recorded_adis_account_polls_successfully(stuttgart_config) -> None:
    _mock_stuttgart()
    result = await poll_account(stuttgart_config, "hunter2")

    assert result.status is PollStatus.SUCCESS
    assert len(result.loans) == 11
    # aDIS fees are not implemented; that must not read as "no fees owed".
    assert result.fees_supported is False


@respx.mock
async def test_an_adis_session_that_fell_back_to_the_search_mask_is_a_parse_error(stuttgart_config) -> None:
    """aDIS answers an expired session with the search mask, which carries no
    loan listing and would otherwise parse as an empty account."""
    _mock_stuttgart("stuttgart_home.html")
    result = await poll_account(stuttgart_config, "hunter2")

    assert result.status is PollStatus.PARSE_ERROR
    assert result.loans == []


@respx.mock
async def test_detail_enrichment_over_recorded_catalogue_pages(account_config) -> None:
    """fetch_details is opt-in and best-effort; over the real pages it has to
    fill in an ISBN where there is one and leave a product GTIN alone."""
    _mock_remseck("remseck_checkouts.html")
    respx.get(url__startswith=f"{REMSECK}/cgi-bin/koha/opac-detail.pl").mock(
        return_value=httpx.Response(200, html=recorded_fixture("remseck_detail.html"))
    )
    result = await poll_account(account_config, "hunter2", fetch_details=True)

    assert result.status is PollStatus.SUCCESS
    assert all(loan.isbn == "9783473460625" for loan in result.loans)


@respx.mock
async def test_a_product_gtin_is_not_recorded_as_an_isbn(account_config) -> None:
    """A tiptoi puzzle's EAN starts 4005, not 978/979. Storing it as an ISBN
    would send every price and cover lookup after an unrelated product."""
    _mock_remseck("remseck_checkouts.html")
    respx.get(url__startswith=f"{REMSECK}/cgi-bin/koha/opac-detail.pl").mock(
        return_value=httpx.Response(200, html=recorded_fixture("remseck_detail_ean.html"))
    )
    result = await poll_account(account_config, "hunter2", fetch_details=True)

    assert result.status is PollStatus.SUCCESS
    assert all(loan.isbn is None for loan in result.loans)


# --- what the real rows do to the identity ladder ------------------------


@respx.mock
async def test_every_recorded_koha_loan_gets_a_distinct_copy_key(account_config) -> None:
    """Two loans collapsing onto one key would have the reconciler see a
    return and a fresh loan where nothing happened."""
    _mock_remseck("remseck_checkouts.html")
    result = await poll_account(account_config, "hunter2")

    keys = [
        copy_key(
            "remseck",
            title=loan.title,
            item_id=loan.item_id,
            barcode=loan.barcode,
            call_number=loan.call_number,
            author=loan.author,
            media_type=loan.media_type,
        )
        for loan in result.loans
    ]
    assert len(set(keys)) == len(keys)


@respx.mock
async def test_every_recorded_adis_loan_gets_a_distinct_copy_key(stuttgart_config) -> None:
    """Stuttgart's ladder is the interesting one: books key on a barcode,
    while CDs and games have none and fall through to call number plus title.
    Three of the recorded games share the call number "Spiel"."""
    _mock_stuttgart()
    result = await poll_account(stuttgart_config, "hunter2")

    keys = [
        copy_key(
            "stuttgart",
            title=loan.title,
            item_id=loan.item_id,
            barcode=loan.barcode,
            call_number=loan.call_number,
            author=loan.author,
            media_type=loan.media_type,
        )
        for loan in result.loans
    ]
    assert len(set(keys)) == len(keys)


@respx.mock
async def test_a_koha_subtitle_reaches_the_work_key_separated(account_config) -> None:
    """Koha runs the title and subtitle together unless the backend separates
    them, and a work key built on the run-together string is a work nobody
    else will ever name the same way."""
    _mock_remseck("remseck_checkouts.html")
    result = await poll_account(account_config, "hunter2")

    titles = {loan.title for loan in result.loans}
    mangled = "tiptoi Puzzle für kleine Entdecker: ZooKinderpuzzle ab 3 Jahren, für 1 Spieler"
    fixed = "tiptoi Puzzle für kleine Entdecker: Zoo : Kinderpuzzle ab 3 Jahren, für 1 Spieler"
    assert mangled not in titles
    assert fixed in titles
    assert media_key(MediaClass.GAME, fixed) != media_key(MediaClass.GAME, mangled)


# --- the media vocabulary these libraries actually use -------------------


@respx.mock
async def test_the_recorded_adis_media_types_all_classify(stuttgart_config) -> None:
    _mock_stuttgart()
    result = await poll_account(stuttgart_config, "hunter2")

    for loan in result.loans:
        assert classify(loan.media_type, loan.call_number) is not MediaClass.OTHER

    by_title = {loan.title: loan for loan in result.loans}
    game = by_title["Medical Mysteries - Miami Flatline"]
    assert classify(game.media_type, game.call_number) is MediaClass.GAME


@respx.mock
async def test_koha_reports_no_lending_date_so_history_must_be_derived(account_config) -> None:
    """This Koha renders no checkout-date column. The lending date has to come
    from the poll that first saw the loan, which is what reconcile() is for --
    a consumer trusting checkout_date would record every loan as undated."""
    _mock_remseck("remseck_checkouts.html")
    result = await poll_account(account_config, "hunter2")

    assert all(loan.checkout_date is None for loan in result.loans)
    assert all(row["checkout_date"] is None for row in result.serialised_loans())
