"""The six shop front-ends, the price ladder and the cover ladder.

Every payload here is a verbatim capture from the live site under
`tests/fixtures/providers/` (recorded with the headers the shop demands);
the tests below pin the parsers to those captures. A shop that changes its
markup should break the matching test, not silently return nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata import build_provider
from bib_tracker.metadata.base import MediaQuery, ProviderConfig
from bib_tracker.metadata.http import CachedClient
from bib_tracker.metadata.matcher import choose
from bib_tracker.metadata.pricing import (
    Price,
    PriceBasis,
    all_found_prices,
    price_search_exhausted,
    record_probe,
    search_price,
    store_price,
)
from bib_tracker.metadata.shops import ShopBlocked

PROVIDERS = Path(__file__).parent / "fixtures" / "providers"

BLANK: dict[str, Any] = {"data": {"count": 0, "products": []}}


def payload(name: str) -> str:
    return (PROVIDERS / name).read_text(encoding="utf-8", errors="replace")


EBOOKDE_PRODUCT = (
    "/de/product/42335932/philipp_blom_die_unterwerfung_anfang_und_ende_"
    "der_menschlichen_herrschaft_ueber_die_natur.html"
)


#: The Houellebecq novel: on Thalia's and Buchkatalog's shelves.
HOU = MediaQuery(media_class=MediaClass.BOOK, title="Unterwerfung", author="Michel Houellebecq", isbn="9783462056344")
#: The Blom essay: on buch7's, Lehmanns' and eBook.de's shelves.
BLOM = MediaQuery(media_class=MediaClass.BOOK, title="Die Unterwerfung", author="Philipp Blom", isbn="9783446274211")


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient() as client:
        yield CachedClient(db, client)


@pytest.fixture
async def media_id(db):
    async with db.write() as w:
        return await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, author, isbn13,
                               first_seen_at, last_seen_at)
            VALUES ('shop', 'book', 'Die Unterwerfung', 'Philipp Blom', '9783446274211',
                    datetime('now'), datetime('now'))
            """
        )


def _shop(name: str, http, **kwargs: Any):
    return build_provider(
        ProviderConfig(name=name, base_url=f"https://www.{_HOST[name]}", rate_limit_per_minute=6000, **kwargs),
        http,
    )


_HOST = {
    "thalia": "thalia.de",
    "buchkatalog": "buchkatalog.de",
    "amazon": "amazon.de",
    "buch7": "buch7.de",
    "lehmanns": "lehmanns.de",
    "ebookde": "ebook.de",
}


def _mount_all(m: respx.MockRouter) -> None:
    """Every shop route, serving its capture keyed by the requested term."""
    m.get("https://www.thalia.de/suche").mock(
        side_effect=lambda req: httpx.Response(
            200,
            text=payload("thalia_search_isbn.html")
            if req.url.params["sq"] == "9783462056344"
            else payload("thalia_search_title.html"),
        )
    )
    m.get(url__startswith="https://www.thalia.de/shop/home/artikeldetails/").mock(
        return_value=httpx.Response(200, text=payload("thalia_detail.html"))
    )

    def buchkatalog(req: httpx.Request) -> httpx.Response:
        term = req.url.params["search"]
        if term == "9783462056344":
            return httpx.Response(200, text=payload("buchkatalog_search_isbn.json"))
        if term == "9783446274211":
            return httpx.Response(200, text=payload("buchkatalog_search_blom.json"))
        if term == "Unterwerfung Michel Houellebecq":
            return httpx.Response(200, text=payload("buchkatalog_search.json"))
        if term == "4099276460851090106":
            # The live site answers an id query with nothing (verified); the
            # ladder relies on the record the search already attached.
            return httpx.Response(200, text=json.dumps(BLANK))
        return httpx.Response(200, text=json.dumps(BLANK))

    m.get("https://www.buchkatalog.de/api/search/search").mock(side_effect=buchkatalog)
    m.get("https://www.amazon.de/s").mock(return_value=httpx.Response(200, text=payload("amazon_search_isbn.html")))
    m.get("https://www.amazon.de/dp/B01N5HZ0H6").mock(
        return_value=httpx.Response(404, text=payload("amazon_dp_B01N5HZ0H6.html"))
    )
    m.get("https://www.buch7.de/suche").mock(
        side_effect=lambda req: httpx.Response(
            200,
            text=payload("buch7_search_isbn.html")
            if req.url.params.get("search") == "9783446274211"
            else payload("buch7_search_title.html"),
        )
    )
    m.get(url__regex=r"https://www\.buch7\.de/produkt/.*").mock(
        return_value=httpx.Response(200, text=payload("buch7_detail.html"))
    )
    m.get("https://www.lehmanns.de/search/quick").mock(
        side_effect=lambda req: httpx.Response(
            200,
            text=payload("lehmanns_search_isbn.html")
            if req.url.params["q"] == "9783446274211"
            else payload("lehmanns_search_title.html"),
        )
    )
    m.get(url__regex=r"https://www\.lehmanns\.de/shop/.*").mock(
        return_value=httpx.Response(200, text=payload("lehmanns_detail.html"))
    )
    m.get("https://www.ebook.de/de/search").mock(
        side_effect=lambda req: httpx.Response(
            200,
            text=payload("ebookde_search_isbn.html")
            if req.url.params["q"] == "9783446274211"
            else payload("ebookde_search_title.html"),
        )
    )
    m.get(url__regex=r"https://www\.ebook\.de/de/product/.*").mock(
        return_value=httpx.Response(200, text=payload("ebookde_detail.html"))
    )


# -- Thalia ------------------------------------------------------------------


@respx.mock
async def test_thalia_falls_back_to_the_title_when_the_isbn_finds_no_tiles(http) -> None:
    """The recorded ISBN page carries zero product tiles; the title page answers."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("thalia", http)
        candidates = await provider.search(HOU)

    assert candidates[0].external_id == "A1076573313"
    assert candidates[0].title == "Unterwerfung"
    assert candidates[0].authors == ["Michel Houellebecq"]
    # The tile shows the price as text only; it is kept for display, and the
    # real figure comes from the detail page's JSON-LD.
    assert candidates[0].payload["sale_price_text"] == "15,75 €"
    assert candidates[0].payload["list_price_text"] == "22,99 €"


@respx.mock
async def test_thalia_detail_states_the_price_cover_and_gtin(http) -> None:
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("thalia", http)
        record = await provider.fetch("A1076573313")

    assert record is not None
    assert record.list_price_cents == 1575
    assert record.list_price_currency == "EUR"
    assert record.cover_source_url is not None
    assert record.cover_source_url.startswith("https://images.thalia.media/")
    assert record.isbn13 == "2710001282388"
    assert record.description and "umstrittenste Roman" in record.description
    assert record.external_url == "https://www.thalia.de/shop/home/artikeldetails/A1076573313"


@respx.mock
async def test_thalia_says_blocked_rather_than_empty_when_the_wall_answers(http) -> None:
    """A 403 from the bot wall must not be recorded as 'this shop has it not'."""
    respx.get("https://www.thalia.de/suche").mock(
        return_value=httpx.Response(403, text=payload("thalia_blocked_403.html"))
    )
    provider = _shop("thalia", http)

    with pytest.raises(ShopBlocked):
        await provider.search(HOU)


# -- Buchkatalog.de ----------------------------------------------------------


@respx.mock
async def test_buchkatalog_answers_with_the_price_inline(http) -> None:
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("buchkatalog", http)
        candidates = await provider.search(HOU)

    best = candidates[0]
    assert best.external_id == "4099276460851090106"
    assert best.title == "Unterwerfung"
    assert best.authors == ["Michel Houellebecq"]
    assert best.isbn13 == "9783832163594"
    # The JSON answer carries price and cover outright, so the ladder reads
    # them without a second request.
    assert best.record is not None
    assert best.record.list_price_cents == 1500
    assert best.record.cover_source_url == "https://www.buchkatalog.de/media-cover/cover/55/39/53/5539536700001N.jpg"


@respx.mock
async def test_buchkatalog_id_lookup_is_honest_about_being_empty(http) -> None:
    """Recorded reality: an id query answers 0 products; the ladder keeps the
    record the search attached instead of chasing a fetch."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("buchkatalog", http)
        assert await provider.fetch("4099276460851090106") is None


# -- Amazon.de ----------------------------------------------------------------


@respx.mock
async def test_amazon_skips_the_sponsored_cards(http) -> None:
    """The recorded results page holds three cards and all three are ads;
    Amazon answered 'nothing organic here', which is not 'not stocked'."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("amazon", http)
        assert await provider.search(MediaQuery(media_class=MediaClass.BOOK, title="", isbn="9783462056344")) == []


@respx.mock
async def test_amazon_reports_the_interstitial_as_blocked(http) -> None:
    respx.get("https://www.amazon.de/s").mock(
        return_value=httpx.Response(200, text=payload("amazon_interstitial.html"))
    )
    provider = _shop("amazon", http)

    with pytest.raises(ShopBlocked):
        await provider.search(HOU)


@respx.mock
async def test_amazon_detail_page_that_says_nothing_gives_no_record(http) -> None:
    """The recorded /dp page is a 404-shaped 'not found' page, not a wall."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("amazon", http)
        assert await provider.fetch("B01N5HZ0H6") is None


# -- buch7.de ------------------------------------------------------------------


@respx.mock
async def test_buch7_recognises_the_isbn_redirect_to_the_product_page(http) -> None:
    """An ISBN search 302s to the product page; the follow is invisible to the
    caching client, and the page's og:url is what says the answer is a product."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("buch7", http)
        candidates = await provider.search(BLOM)

    assert candidates[0].external_id == "/produkt/die-unterwerfung-philipp-blom/1043238278?ean=9783446274211"
    assert candidates[0].title == "Die Unterwerfung"
    assert candidates[0].isbn13 == "9783446274211"
    assert candidates[0].record is not None
    assert candidates[0].record.list_price_cents == 2800


@respx.mock
async def test_buch7_title_list_needs_the_detail_page_for_a_price(http) -> None:
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("buch7", http)
        query = MediaQuery(media_class=MediaClass.BOOK, title="Die Unterwerfung", author="Philipp Blom")
        candidates = await provider.search(query)
        assert candidates[0].record is None

        record = await provider.fetch(candidates[0].external_id)

    assert record is not None
    assert record.list_price_cents == 2800
    assert record.cover_source_url and record.cover_source_url.startswith("https://medias.librinet.de/")


# -- Lehmanns.de ----------------------------------------------------------------


@respx.mock
async def test_lehmanns_tiles_answer_without_a_price(http) -> None:
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("lehmanns", http)
        candidates = await provider.search(BLOM)

    best = candidates[0]
    assert best.external_id == "/shop/geisteswissenschaften/58887543-9783446274211-die-unterwerfung"
    assert best.title == "Die Unterwerfung"
    assert best.isbn13 == "9783446274211"
    # Recorded reality: Lehmanns renders its tiles without a price (it is
    # filled in client-side), so the ladder has to take the detail page.
    assert best.record is None


@respx.mock
async def test_lehmanns_isbn_search_follows_the_product_link_it_leaves_behind(http) -> None:
    """The quick search answers 0 tiles for the ISBN but links the product;
    the price only exists in the detail page's add-to-cart gtag label."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("lehmanns", http)
        candidates = await provider.search(MediaQuery(media_class=MediaClass.BOOK, title="", isbn="9783446274211"))
        assert candidates[0].record is None

        record = await provider.fetch(candidates[0].external_id)

    assert record is not None
    assert record.list_price_cents == 2800


@respx.mock
async def test_ebookde_isbn_search_answers_with_the_matching_tile(http) -> None:
    """The ISBN page's tiles are all recommendation carousels except one —
    the book itself. The matcher, not the tile order, is what picks it."""
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("ebookde", http)
        isbn_only = MediaQuery(media_class=MediaClass.BOOK, title="", author=None, isbn="9783446274211")
        candidates = await provider.search(isbn_only)

    assert len(candidates) > 1  # the page is mostly recommendations
    best, _ = choose(isbn_only, candidates)
    assert best is not None
    assert best.external_id == EBOOKDE_PRODUCT
    assert best.isbn13 == "9783446274211"  # from the coverscan filename, the tile's only ISBN
    assert best.record is not None
    assert best.record.list_price_cents == 2800


@respx.mock
async def test_ebookde_detail_page_states_price_cover_and_publisher(http) -> None:
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        provider = _shop("ebookde", http)
        record = await provider.fetch(EBOOKDE_PRODUCT)

    assert record is not None
    assert record.list_price_cents == 2800
    assert record.cover_source_url == "https://media2.ebook.de/shop/coverscans/423/42335932_9783446274211_xl.jpg"
    assert record.isbn13 == "9783446274211"
    assert record.publisher == "Carl Hanser Verlag"


def test_a_shop_never_fills_a_bibliographic_field() -> None:
    """The merge table names no shop: a shop's description or cover must not
    overwrite the catalogue's, no matter how fresh its probe is."""
    from bib_tracker.metadata.base import ProviderRecord, ProviderStatus
    from bib_tracker.metadata.merge import merge_records

    shop = ProviderRecord(
        provider="thalia",
        external_id="A1",
        status=ProviderStatus.OK,
        description="Werbung",
        publisher="Thalia",
        cover_source_url="https://images.thalia.media/x.jpg",
        list_price_cents=1575,
    )
    catalogue = ProviderRecord(
        provider="googlebooks",
        external_id="G1",
        status=ProviderStatus.OK,
        description="Klappentext",
        publisher="Kiepenheuer & Witsch",
    )

    merged = merge_records([shop, catalogue])

    assert merged["description"] == "Klappentext"
    assert merged["publisher"] == "Kiepenheuer & Witsch"
    assert "cover_source_url" not in merged or merged["cover_source_url"] is None


# -- the ladder -----------------------------------------------------------------


@respx.mock
async def test_the_ladder_asks_every_configured_shop_and_keeps_every_answer(db, settings, http, media_id) -> None:
    """A found price does not stop the walk: the comparison list needs every
    platform, and the probe table is what keeps the extra requests one-time."""
    settings.price_providers = ["buch7", "lehmanns", "ebookde"]
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        price = await search_price(db, settings, http, media_id, BLOM)
        assert price is not None
        assert price.cents == 2800
        assert price.provider == "buch7"  # first in the configured order, though nobody is cheaper

        probes = {
            row["provider"]: row["outcome"]
            for row in await db.fetch_all("SELECT provider, outcome FROM lookup_probes WHERE media_id = ?", (media_id,))
        }
        assert probes == {"buch7": "found", "lehmanns": "found", "ebookde": "found"}

        found = await all_found_prices(db, settings, media_id)
        assert [name for name, _ in found] == ["buch7", "lehmanns", "ebookde"]
        assert [price.cents for _, price in found] == [2800, 2800, 2800]

        calls_before = sum(route.calls.call_count for route in m.routes)
        again = await search_price(db, settings, http, media_id, BLOM)
        assert again is not None and again.cents == 2800
        assert sum(route.calls.call_count for route in m.routes) == calls_before


@respx.mock
async def test_the_first_configured_source_wins_even_against_a_cheaper_one(db, settings, http, media_id) -> None:
    settings.price_providers = ["thalia", "buchkatalog"]
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        price = await search_price(db, settings, http, media_id, HOU)

    assert price is not None
    assert (price.cents, price.provider) == (1575, "thalia")  # 15,75 over Buchkatalog's 15,00
    row = await db.fetch_one("SELECT effective_price_cents, price_provider FROM media WHERE id = ?", (media_id,))
    assert (row["effective_price_cents"], row["price_provider"]) == (1575, "thalia")
    found = await all_found_prices(db, settings, media_id)
    assert [(name, p.cents) for name, p in found] == [("thalia", 1575), ("buchkatalog", 1500)]


@respx.mock
async def test_a_blocked_shop_is_marked_and_the_ladder_walks_on(db, settings, http, media_id) -> None:
    settings.price_providers = ["thalia", "buchkatalog"]
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    m.get("https://www.thalia.de/suche").mock(return_value=httpx.Response(403, text=payload("thalia_blocked_403.html")))
    with m:
        price = await search_price(db, settings, http, media_id, HOU)

    assert price is not None
    assert (price.cents, price.provider) == (1500, "buchkatalog")
    probe = await db.fetch_one(
        "SELECT outcome FROM lookup_probes WHERE media_id = ? AND provider = 'thalia'", (media_id,)
    )
    assert probe is not None and probe["outcome"] == "blocked"


@respx.mock
async def test_a_price_you_typed_survives_the_ladder(db, settings, http, media_id) -> None:
    settings.price_providers = ["buchkatalog"]
    await store_price(db, media_id, Price(999, PriceBasis.MANUAL), source="manual")
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        assert await search_price(db, settings, http, media_id, HOU) is None

    row = await db.fetch_one("SELECT effective_price_cents, price_basis FROM media WHERE id = ?", (media_id,))
    assert row["effective_price_cents"] == 999
    assert row["price_basis"] == "manual"


@respx.mock
async def test_exhaustion_is_marked_only_when_nothing_answered(db, settings, http, media_id) -> None:
    settings.price_providers = ["amazon"]
    m = respx.mock(assert_all_called=False)
    _mount_all(m)
    with m:
        assert await search_price(db, settings, http, media_id, HOU) is None

    assert await price_search_exhausted(db, settings, media_id) is True
    probe = await db.fetch_one(
        "SELECT outcome FROM lookup_probes WHERE media_id = ? AND provider = 'amazon'", (media_id,)
    )
    assert probe is not None and probe["outcome"] == "not_found"

    # One answer anywhere — a catalogue record included — undoes it.
    async with db.write() as w:
        await w.execute(
            "INSERT INTO metadata_records (media_id, provider, status, list_price_cents, fetched_at)"
            " VALUES (?, 'dnb', 'ok', 1495, datetime('now'))",
            (media_id,),
        )
    assert await price_search_exhausted(db, settings, media_id) is False


async def test_the_media_page_names_the_source_and_lists_the_others(db, settings, client, media_id) -> None:
    await store_price(
        db, media_id, Price(1575, PriceBasis.PROVIDER, provider="thalia"), source="thalia", price_provider="thalia"
    )
    await store_price(
        db, media_id, Price(1500, PriceBasis.PROVIDER, provider="buchkatalog"), source="buchkatalog", update_media=False
    )

    page = (await client.get(f"/media/{media_id}")).text

    assert "Listenpreis von Thalia" in page
    assert "Buchkatalog.de" in page
    assert "15,00" in page  # the comparison entry, formatted German-style


async def test_a_manual_price_says_so_on_the_page(db, settings, client, media_id) -> None:
    await store_price(db, media_id, Price(999, PriceBasis.MANUAL), source="manual")

    assert "von dir eingetragen" in (await client.get(f"/media/{media_id}")).text


async def test_the_exhausted_mark_shows_on_the_page(db, settings, client, media_id) -> None:
    settings.price_providers = ["buch7"]
    await record_probe(db, media_id, "buch7", "price", "not_found")

    assert "keine Quelle gefunden" in (await client.get(f"/media/{media_id}")).text


# -- the worker's cover precedence ------------------------------------------------


async def test_the_librarys_cover_beats_the_image_ladder(db, settings, media_id) -> None:
    """The OPAC stated a cover, so nothing may ask the shops for one."""
    import io

    from PIL import Image

    from bib_tracker.metadata.worker import EnrichmentWorker

    def png() -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (300, 450), (20, 90, 160)).save(buffer, format="PNG")
        return buffer.getvalue()

    now = "2026-01-01 00:00:00"
    async with db.write() as w:
        account = await w.execute("INSERT INTO accounts (name, library_type, username) VALUES ('bib', 'koha', 'u')")
        run = await w.execute(
            "INSERT INTO poll_runs (account_id, trigger, status, started_at) VALUES (?, 'import', 'success', ?)",
            (account, now),
        )
        await w.execute(
            "INSERT INTO snapshot_items (run_id, account_id, observed_at, copy_key, raw_json, title, due_date,"
            " cover_url) VALUES (?, ?, ?, 'c1', '{}', 'Die Unterwerfung', ?, ?)",
            (run, account, now, now, "https://covers.example.com/x.jpg"),
        )
        await w.execute(
            "INSERT INTO copies (account_id, copy_key, media_id, library_type, first_seen_at, last_seen_at)"
            " VALUES (?, 'c1', ?, 'koha', ?, ?)",
            (account, media_id, now, now),
        )

    settings.metadata_providers = []
    settings.price_providers = []
    settings.image_providers = ["thalia"]

    m = respx.mock(assert_all_called=True)
    m.get("https://covers.example.com/x.jpg").mock(return_value=httpx.Response(200, content=png()))
    async with httpx.AsyncClient() as client:
        with m:
            await EnrichmentWorker(db, settings, client)._apply_results({media_id})

    media = await db.fetch_one("SELECT cover_sha256 FROM media WHERE id = ?", (media_id,))
    assert media is not None and media["cover_sha256"]
    row = await db.fetch_one("SELECT provider FROM images WHERE sha256 = ?", (media["cover_sha256"],))
    assert row is not None and row["provider"] == "library"
    # The ladder saw a cover already present and asked nobody anything.
    probes = await db.fetch_all("SELECT provider FROM lookup_probes WHERE media_id = ?", (media_id,))
    assert probes == []


# -- the CLI ------------------------------------------------------------------------


def test_the_lookup_cli_prints_the_answers_it_found(tmp_path, monkeypatch) -> None:
    """One provider, no database: the probe tool's whole contract.

    Sync test: `lookup` drives its own event loop, and asyncio.run cannot be
    called from the pytest-asyncio one.
    """
    import contextlib
    import io
    import json as jsonlib

    from bib_tracker.cli_lookup import lookup
    from bib_tracker.metadata import PROVIDER_FACTORIES
    from bib_tracker.metadata.base import (
        BaseProvider,
        ProviderCandidate,
        ProviderRecord,
        ProviderStatus,
    )

    class Stub(BaseProvider):
        name = "stub"

        async def search(self, query: MediaQuery) -> list[ProviderCandidate]:
            return [
                ProviderCandidate(
                    external_id="stub-1",
                    title=query.title,
                    record=ProviderRecord(
                        provider="stub",
                        external_id="stub-1",
                        status=ProviderStatus.OK,
                        list_price_cents=1500,
                        description="Klappentext",
                        cover_source_url="https://covers.example.com/x.jpg",
                    ),
                )
            ]

        async def fetch(self, external_id: str) -> ProviderRecord | None:
            return None

    monkeypatch.setitem(PROVIDER_FACTORIES, "stub", Stub)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = lookup(["9783462056344", "--provider", "stub", "--json", "--fresh"])

    assert code == 0
    rows = jsonlib.loads(buffer.getvalue())
    assert rows[0]["provider"] == "stub"
    assert rows[0]["price_cents"] == 1500
    assert rows[0]["description"] == "Klappentext"
    assert rows[0]["cover_url"] == "https://covers.example.com/x.jpg"
