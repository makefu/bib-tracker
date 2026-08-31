"""Rating your own borrows."""

from __future__ import annotations

import httpx
import respx

from tests.conftest import library_fixture

BASE_URL = "http://opac.test"


def _mock(checkouts: str) -> None:
    respx.post(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{BASE_URL}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )


async def _first_media_id(api: httpx.AsyncClient) -> int:
    loans = (await api.get("/api/history")).json()["loans"]
    return int(loans[0]["media_id"])


@respx.mock
async def test_rating_a_work_persists_and_returns_the_widget(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    media_id = await _first_media_id(api)

    response = await api.post(f"/api/media/{media_id}/rating", data={"rating": "4"})

    assert response.status_code == 200
    # The swap target comes back with the new value already selected.
    assert 'value="4"' in response.text
    assert "checked" in response.text
    assert (await api.get(f"/media/{media_id}")).text.count("checked") >= 1


@respx.mock
async def test_a_rating_can_be_cleared(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    media_id = await _first_media_id(api)

    await api.post(f"/api/media/{media_id}/rating", data={"rating": "5"})
    await api.post(f"/api/media/{media_id}/rating", data={"rating": "0"})

    body = (await api.get(f"/media/{media_id}")).text
    assert "checked" not in body


@respx.mock
async def test_an_out_of_range_rating_is_refused(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    media_id = await _first_media_id(api)

    assert (await api.post(f"/api/media/{media_id}/rating", data={"rating": "9"})).status_code == 400
    assert (await api.post(f"/api/media/{media_id}/rating", data={"rating": "x"})).status_code == 400


@respx.mock
async def test_notes_are_saved_against_the_work(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    media_id = await _first_media_id(api)

    await api.post(f"/api/media/{media_id}/notes", data={"review": "Beim zweiten Mal besser."})

    assert "Beim zweiten Mal besser." in (await api.get(f"/media/{media_id}")).text


@respx.mock
async def test_the_queue_offers_returned_but_unrated_items(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    _mock(library_fixture("remseck_checkouts_returned.html"))
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/rate")).text
    assert "Die unendliche Geschichte" in body
    assert "1 offen" in body


@respx.mock
async def test_an_item_still_on_loan_is_not_in_the_queue(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/rate")).text
    assert "Alles bewertet" in body


@respx.mock
async def test_rating_removes_an_item_from_the_queue(api: httpx.AsyncClient) -> None:
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    _mock(library_fixture("remseck_checkouts_returned.html"))
    await api.post("/api/accounts/remseck/poll")

    returned = [loan for loan in (await api.get("/api/history")).json()["loans"] if loan["state"] == "returned"]
    await api.post(f"/api/media/{returned[0]['media_id']}/rating", data={"rating": "3"})

    assert "Alles bewertet" in (await api.get("/rate")).text


@respx.mock
async def test_dismissing_stops_the_prompt_without_inventing_a_rating(api: httpx.AsyncClient) -> None:
    """ "Never ask again" must not show up later as a rating you never gave."""
    _mock(library_fixture("remseck_checkouts.html"))
    await api.post("/api/accounts/remseck/poll")
    _mock(library_fixture("remseck_checkouts_returned.html"))
    await api.post("/api/accounts/remseck/poll")

    returned = [loan for loan in (await api.get("/api/history")).json()["loans"] if loan["state"] == "returned"]
    media_id = returned[0]["media_id"]
    await api.post(f"/api/media/{media_id}/dismiss")

    assert "Alles bewertet" in (await api.get("/rate")).text
    body = (await api.get(f"/media/{media_id}")).text
    assert "checked" not in body
