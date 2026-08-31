"""Statistics, and the honesty rules they have to keep."""

from __future__ import annotations

import httpx
import pytest
import respx

from bib_tracker import stats
from bib_tracker.db import queries
from bib_tracker.library.reconcile import reconcile_pending
from bib_tracker.metadata.pricing import Price, PriceBasis, store_price
from tests.conftest import library_fixture
from tests.test_reconcile import ACCOUNT, DAY, T0, loan, record_poll

OPAC = "http://opac.test"


@pytest.fixture
async def account(db):
    await queries.sync_accounts(db, [ACCOUNT])
    row = await queries.get_account(db, "remseck")
    return row.id


class Timeline:
    """Lays polls out in order, so each loan is a distinct episode.

    Polls daily while an item is out, the way a real poller does. With only two
    polls a loan's computed duration collapses to zero, because all that is
    actually known is "it was there, then it was not" -- and the return date is
    deliberately taken at the earlier bound so a long gap between polls can
    never inflate a duration.
    """

    def __init__(self) -> None:
        self.offset = 0

    async def completed_loan(self, db, settings, account, title: str, item_id: str, days: int) -> None:
        start = T0 + self.offset * DAY
        item = loan(title, item_id=item_id, due="2026-12-29", checkout_date=start.date().isoformat())

        for day in range(days + 1):
            await record_poll(db, [item], at=start + day * DAY, account_id=account)
            await reconcile_pending(db, settings)

        await record_poll(db, [], at=start + (days + 1) * DAY, account_id=account)
        await reconcile_pending(db, settings)

        # Well clear of the re-open window, so the next loan is a new episode.
        self.offset += days + 7


@pytest.fixture
def timeline() -> Timeline:
    return Timeline()


async def _completed_loan(db, settings, account, title: str, item_id: str, days: int) -> None:
    await Timeline().completed_loan(db, settings, account, title, item_id, days)


# -- money ------------------------------------------------------------------


async def test_money_saved_always_reports_its_basis(db, settings, account) -> None:
    """The whole point: a figure built on guesses must say how much of it is."""
    await _completed_loan(db, settings, account, "Momo", "1", 10)
    media = await db.fetch_one("SELECT id FROM media")
    await store_price(db, media["id"], Price(1500, PriceBasis.DEFAULT), source="default_by_class")

    result = (await stats.money_saved(db, settings)).as_dict()

    assert result["total_cents"] == 1500
    assert result["estimated_cents"] == 1500
    assert result["exact_cents"] == 0
    assert result["basis"] == {"exact": 0, "estimated": 1}
    assert result["exact_share"] == 0.0


async def test_a_real_list_price_counts_as_exact(db, settings, account) -> None:
    await _completed_loan(db, settings, account, "Momo", "1", 10)
    media = await db.fetch_one("SELECT id FROM media")
    await store_price(db, media["id"], Price(2400, PriceBasis.PROVIDER), source="googlebooks")

    result = await stats.money_saved(db, settings)

    assert result.exact_cents == 2400
    assert result.estimated_cents == 0
    assert result.exact_share == 1.0
    assert result.is_mostly_estimated is False


async def test_a_price_you_typed_counts_as_exact(db, settings, account) -> None:
    await _completed_loan(db, settings, account, "Momo", "1", 10)
    media = await db.fetch_one("SELECT id FROM media")
    await store_price(db, media["id"], Price(1899, PriceBasis.MANUAL), source="manual")

    assert (await stats.money_saved(db, settings)).exact_cents == 1899


async def test_items_still_on_loan_are_not_counted_as_saved(db, settings, account) -> None:
    """Nothing has been saved yet on a book that is still in the hallway."""
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-29")], at=T0, account_id=account)
    await reconcile_pending(db, settings)
    media = await db.fetch_one("SELECT id FROM media")
    await store_price(db, media["id"], Price(1500, PriceBasis.DEFAULT), source="default_by_class")

    assert (await stats.money_saved(db, settings)).total_cents == 0


# -- durations --------------------------------------------------------------


async def test_durations_exclude_loans_with_an_unknowable_start(db, settings, account) -> None:
    """A loan already running when tracking began has no duration, and
    counting it as zero would drag every average down."""
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-03-29", times_renewed=2)],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, settings)
    await record_poll(db, [], at=T0 + 10 * DAY, account_id=account)
    await reconcile_pending(db, settings)

    result = await stats.durations(db)

    assert result.counted == 0
    assert result.excluded == 1
    assert result.median_days is None


async def test_durations_report_the_spread_not_just_an_average(db, settings, account, timeline) -> None:
    for index, days in enumerate((5, 10, 20, 40)):
        await timeline.completed_loan(db, settings, account, f"Buch {index}", str(index), days)

    result = await stats.durations(db)

    assert result.counted == 4
    assert result.median_days == 15.0
    assert result.p25_days is not None and result.p25_days < result.median_days
    assert result.p75_days is not None and result.p75_days > result.median_days
    assert result.by_class[0]["media_class"] == "book"


# -- the rest ---------------------------------------------------------------


async def test_media_mix_shares_add_up(db, settings, account, timeline) -> None:
    await timeline.completed_loan(db, settings, account, "Momo", "1", 5)
    await timeline.completed_loan(db, settings, account, "Azul", "2", 5)

    mix = await stats.media_mix(db)

    assert sum(entry["count"] for entry in mix) == 2
    assert abs(sum(entry["share"] for entry in mix) - 1.0) < 1e-9


async def test_renewal_rate_is_computed_over_every_loan(db, settings, account) -> None:
    await record_poll(
        db,
        [
            loan("Momo", item_id="1", due="2026-03-29", times_renewed=2),
            loan("Krabat", item_id="2", due="2026-03-29"),
        ],
        at=T0,
        account_id=account,
    )
    await reconcile_pending(db, settings)

    result = await stats.renewal_behaviour(db)

    assert result["total"] == 2
    assert result["renewed"] == 1
    assert result["rate"] == 0.5


async def test_overdue_history_uses_the_latched_flag(db, settings, account) -> None:
    """Not recomputed from the current due date, which a renewal moves."""
    await record_poll(db, [loan("Momo", item_id="1", due="2026-03-01")], at=T0 + 5 * DAY, account_id=account)
    await reconcile_pending(db, settings)
    await record_poll(
        db,
        [loan("Momo", item_id="1", due="2026-12-01", times_renewed=1)],
        at=T0 + 10 * DAY,
        account_id=account,
    )
    await reconcile_pending(db, settings)

    result = await stats.overdue_history(db)

    assert result["overdue"] == 1
    assert result["worst_days"] == 5


async def test_repeat_borrows_finds_works_taken_out_more_than_once(db, settings, account, timeline) -> None:
    await timeline.completed_loan(db, settings, account, "Momo", "1", 5)
    await timeline.completed_loan(db, settings, account, "Momo", "1", 7)

    repeats = await stats.repeat_borrows(db)

    assert len(repeats) == 1
    assert repeats[0]["title"] == "Momo"
    assert repeats[0]["borrows"] == 2


async def test_data_quality_counts_what_is_actually_known(db, settings, account) -> None:
    await _completed_loan(db, settings, account, "Momo", "1", 5)

    quality = await stats.data_quality(db)

    assert quality["runs"] >= 2
    assert quality["success_rate"] == 1.0
    assert quality["dates_known"] == 1
    assert quality["dates_unknown"] == 0


async def test_overview_returns_every_panel(db, settings, account) -> None:
    await _completed_loan(db, settings, account, "Momo", "1", 5)

    data = await stats.overview(db, settings)

    assert set(data) == {
        "money",
        "durations",
        "pace",
        "media_mix",
        "renewals",
        "overdue",
        "repeats",
        "cost_per_day",
        "quality",
    }


# -- the page ---------------------------------------------------------------


async def test_the_statistics_page_renders_without_data(client: httpx.AsyncClient) -> None:
    response = await client.get("/stats")
    assert response.status_code == 200
    assert "Statistik" in response.text


@respx.mock
async def test_the_page_labels_an_estimated_total_as_estimated(api: httpx.AsyncClient) -> None:
    checkouts = library_fixture("remseck_checkouts.html")
    respx.post(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )
    await api.post("/api/accounts/remseck/poll")

    body = (await api.get("/stats")).text

    assert "Gespartes Geld" in body
    assert "Gesichert" in body
    assert "Geschätzt" in body
    # The hatched band that makes the estimated share visible in the picture.
    assert "url(#hatch)" in body


# -- export -----------------------------------------------------------------


@respx.mock
async def test_the_csv_export_keeps_each_date_s_provenance(api: httpx.AsyncClient) -> None:
    """Dropping the source columns would turn estimates into facts the moment
    the file leaves the application."""
    checkouts = library_fixture("remseck_checkouts.html")
    respx.post(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-user.pl").mock(return_value=httpx.Response(200, html=checkouts))
    respx.get(f"{OPAC}/cgi-bin/koha/opac-account.pl").mock(
        return_value=httpx.Response(200, html=library_fixture("remseck_no_fees.html"))
    )
    await api.post("/api/accounts/remseck/poll")

    response = await api.get("/export/history.csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    header = response.text.splitlines()[0]
    for column in ("lend_date_source", "lend_date_earliest", "price_basis"):
        assert column in header
    assert "Die unendliche Geschichte" in response.text


async def test_the_json_export_is_empty_without_data(client: httpx.AsyncClient) -> None:
    assert (await client.get("/export/history.json")).json() == {"loans": []}


async def test_the_stats_api_never_reports_a_bare_money_total(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/stats")).json()

    assert "money_saved" in body
    assert set(body["money_saved"]) >= {"total_cents", "exact_cents", "estimated_cents", "basis"}
    assert "excluded_unknown_start" in body["durations"]
