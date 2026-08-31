"""The server-rendered pages."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import library_fixture

BASE_URL = "http://opac.test"


def _mock(checkouts: str) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )


@pytest.mark.parametrize("path", ["/", "/loans", "/history", "/runs"])
async def test_pages_render_without_any_data(client: httpx.AsyncClient, path: str) -> None:
    """A fresh install must not 500 on an empty database."""
    response = await client.get(path)
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text
    assert "bib-tracker" in response.text


async def test_the_shell_links_the_vendored_assets(client: httpx.AsyncClient) -> None:
    body = (await client.get("/")).text
    assert "/static/vendor/htmx.min.js" in body
    assert "/static/vendor/alpine.min.js" in body
    # Nothing may be pulled from a CDN at runtime.
    assert "http://cdn" not in body
    assert "https://cdn" not in body


async def test_an_empty_history_explains_itself(client: httpx.AsyncClient) -> None:
    body = (await client.get("/history")).text
    assert "Keine Ausleihen" in body


async def test_a_skip_link_comes_first(client: httpx.AsyncClient) -> None:
    body = (await client.get("/")).text
    assert body.index('class="skip"') < body.index('class="shell"')


@respx.mock
async def test_history_shows_loans_with_their_date_precision(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    _mock(library_fixture("remseck_checkouts_returned.html"))
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/history")).text

    assert "Die unendliche Geschichte" in body
    assert "zurück" in body
    # A start that predates tracking is marked, not silently shown as a date.
    assert "date--unknown" in body or "date--inferred" in body


@respx.mock
async def test_history_filters_narrow_the_list(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/history", params={"q": "Momo"})).text
    assert "Momo" in body
    assert "Tschick" not in body


@respx.mock
async def test_an_htmx_request_gets_only_the_fragment(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    response = await api.get("/history", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text
    assert 'id="results"' in response.text
    assert "Momo" in response.text


@respx.mock
async def test_a_history_restore_gets_the_whole_page_back(api: httpx.AsyncClient) -> None:
    """Otherwise the back button leaves a bare fragment on screen."""
    response = await api.get(
        "/history",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<!DOCTYPE html>" in response.text


@respx.mock
async def test_the_loans_page_hides_renewal_behind_a_menu(api: httpx.AsyncClient) -> None:
    """Renewals belong to Home Assistant; here they must not be a stray click
    away from a row someone meant to open."""
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/loans")).text
    assert 'aria-haspopup="menu"' in body
    assert "Verlängern" in body
    assert body.index('aria-haspopup="menu"') < body.index("Verlängern")


@respx.mock
async def test_the_media_page_lists_every_borrow_of_a_work(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    loans = (await api.get("/api/history")).json()["loans"]
    media_id = loans[0]["media_id"]

    response = await api.get(f"/media/{media_id}")

    assert response.status_code == 200
    assert loans[0]["title"] in response.text
    # The detail page is where repeat borrows of one work are collected.
    assert "Ausgeliehen" in response.text


async def test_an_unknown_work_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/media/9999")).status_code == 404
