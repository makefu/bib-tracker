"""Renewing loans.

Deliberately a side feature: Home Assistant does the routine renewing, so what
matters here is that a deliberate click is honest about what the library did.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import respx

from tests.conftest import library_fixture

OPAC = "http://opac.test"


def _mock(*, renew_ok: bool = True, fixture: str = "remseck_checkouts.html") -> None:
    html = library_fixture(fixture)
    respx.post(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=html))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=html))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )

    # Koha signals success by redirecting back with renewed=<itemnumber>, so
    # the stub has to echo whichever item was actually posted.
    def renew(request: httpx.Request) -> httpx.Response:
        if not renew_ok:
            return httpx.Response(302, headers={"Location": f"{OPAC}/cgi-bin/koha/opac-user.pl"})
        item = parse_qs(request.content.decode()).get("item", [""])[0]
        return httpx.Response(302, headers={"Location": f"{OPAC}/cgi-bin/koha/opac-user.pl?renewed={item}"})

    respx.post(f"{OPAC}/cgi-bin/koha/opac-renew.pl").mock(side_effect=renew)
    # Detail lookups are best-effort enrichment; keep them out of the way.
    respx.get(f"{OPAC}/cgi-bin/koha/opac-detail.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_detail.html"))
    )


async def _open_loans(api: httpx.AsyncClient) -> list[dict]:
    return [loan for loan in (await api.get("/api/history")).json()["loans"] if loan["state"] == "open"]


@respx.mock
async def test_renewing_one_loan_reports_the_library_s_answer(api: httpx.AsyncClient) -> None:
    _mock()
    await api.post("/api/accounts/remseck/poll")
    loan = (await _open_loans(api))[0]

    response = await api.post(f"/api/loans/{loan['loan_key']}/renew")

    assert response.status_code == 200
    assert "verlängert" in response.text


@respx.mock
async def test_a_refused_renewal_says_so_rather_than_claiming_success(api: httpx.AsyncClient) -> None:
    _mock(renew_ok=False)
    await api.post("/api/accounts/remseck/poll")
    loan = (await _open_loans(api))[0]

    response = await api.post(f"/api/loans/{loan['loan_key']}/renew")

    assert response.status_code == 200
    assert "abgelehnt" in response.text


@respx.mock
async def test_renew_due_covers_overdue_items_too(api: httpx.AsyncClient) -> None:
    """Something already late is past the threshold, not outside it."""
    _mock()
    await api.post("/api/accounts/remseck/poll")

    response = await api.post("/api/accounts/remseck/renew-due")

    assert response.status_code == 200
    assert "verlängert" in response.text


@respx.mock
async def test_renew_due_leaves_alone_what_is_not_due_yet(api: httpx.AsyncClient) -> None:
    _mock()
    await api.post("/api/accounts/remseck/poll")

    # A window far in the past matches nothing, so nothing is attempted.
    response = await api.post("/api/accounts/remseck/renew-due", params={"days": -100000})

    assert response.status_code == 200
    assert "verlängert" not in response.text


@respx.mock
async def test_renewing_re_polls_rather_than_guessing_the_new_due_date(api: httpx.AsyncClient) -> None:
    """The library decides how long an extension runs; only it can say."""
    _mock()
    await api.post("/api/accounts/remseck/poll")
    before = (await api.get("/api/runs/latest")).json()["id"]

    loan = (await _open_loans(api))[0]
    await api.post(f"/api/loans/{loan['loan_key']}/renew")

    after = (await api.get("/api/runs/latest")).json()["id"]
    assert after > before


@respx.mock
async def test_renewing_an_unknown_loan_is_a_404(api: httpx.AsyncClient) -> None:
    _mock()
    assert (await api.post("/api/loans/nope/renew")).status_code == 404


@respx.mock
async def test_renewing_for_an_unknown_account_is_a_404(api: httpx.AsyncClient) -> None:
    _mock()
    assert (await api.post("/api/accounts/nirgendwo/renew-due")).status_code == 404
