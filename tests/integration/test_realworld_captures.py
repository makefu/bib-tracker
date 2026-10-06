"""Providers replayed against what the real shops and catalogues answered.

The captures under ``tests/fixtures/realworld/`` were recorded by
``scripts/record_realworld.py`` from this machine against the live services,
for the corpus of loans the bib.lan instance actually tracks
(``corpus.json``, a verbatim dump of its ``media`` table). They are not
constructed: if a parser passes these, it parsed a page the site really served
about a book this household really borrows.

Replay is offline (respx), so this runs inside ``nix flake check`` with no
network. Refresh the captures with::

    nix develop -c python scripts/record_realworld.py [--provider NAME]...

Two providers are deliberately absent from the found-price set: Thalia is
captured behind its bot wall (403, its manifest carries ``blocked``), and
Google Books' anonymous quota was exhausted at record time -- replaying a
quota error proves nothing about a parser, so googlebooks has no capture dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bib_tracker.library.media_class import MediaClass
from bib_tracker.metadata import PROVIDER_FACTORIES
from bib_tracker.metadata.base import MediaQuery, ProviderConfig
from bib_tracker.metadata.http import CachedClient
from bib_tracker.metadata.pricing import PriceBasis, all_found_prices, search_price
from bib_tracker.metadata.shops import ShopBlocked

REALWORLD = Path(__file__).resolve().parents[1] / "fixtures" / "realworld"

#: The shops whose live answers were captured with a price.
PRICE_SHOPS = ("buchkatalog", "amazon", "buch7", "lehmanns", "ebookde")


def _load(dir_: Path) -> dict[str, Any]:
    return json.loads((dir_ / "manifest.json").read_text(encoding="utf-8"))


def _manifest_dirs() -> list[Path]:
    return sorted(p for p in REALWORLD.iterdir() if (p / "manifest.json").exists())


def _mount(m: respx.MockRouter, manifest: dict[str, Any]) -> list[respx.Route]:
    """Serve every capture for its exact URL; params pick the response."""
    routes: list[respx.Route] = []
    by_url: dict[str, list[dict[str, Any]]] = {}
    for cap in manifest["captures"]:
        by_url.setdefault(cap["url"], []).append(cap)
    for url, caps in by_url.items():
        bodies = [(REALWORLD / manifest["provider"] / cap["file"]).read_bytes() for cap in caps]

        def respond(
            request: httpx.Request, _caps: list[dict[str, Any]] = caps, _bodies: list[bytes] = bodies
        ) -> httpx.Response:
            asked = dict(request.url.params)
            for cap, body in zip(_caps, _bodies, strict=True):
                want = cap.get("params")
                if want and all(asked.get(k) == str(v) for k, v in want.items()):
                    return httpx.Response(cap["status"], content=body)
            return httpx.Response(_caps[0]["status"], content=_bodies[0])

        if caps[0]["method"] == "POST":
            routes.append(m.post(url).mock(side_effect=respond))
        else:
            routes.append(m.get(url).mock(side_effect=respond))
    return routes


def _query(entry: dict[str, Any]) -> MediaQuery:
    return MediaQuery(
        media_class=MediaClass(entry["class"]),
        title=entry["title"],
        author=entry.get("author"),
        isbn=entry.get("isbn"),
    )


def _provider(name: str, http):  # type: ignore[no-untyped-def]
    factory = PROVIDER_FACTORIES[name]
    # The token only decided what the live service answered; the replay
    # parses the body it gave, so a stand-in credential is honest here.
    api_key = "replay" if factory(ProviderConfig(name=name), http).requires_credentials else None
    provider = factory(ProviderConfig(name=name, api_key=api_key, rate_limit_per_minute=60000), http)
    assert provider.available(), f"{name} is unusable even with a credential"
    return provider


@pytest.fixture
async def http(db):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        yield CachedClient(db, client)


async def _match(provider, query: MediaQuery, entry: dict[str, Any]):  # type: ignore[no-untyped-def]
    """The record the provider's own search+fetch pipeline lands on."""
    candidates = await provider.search(query)
    assert candidates, "the provider found nothing on its real answer page"
    asked = entry.get("isbn")
    ordered = sorted(candidates, key=lambda c: 0 if asked and c.isbn13 == asked else 1)
    for candidate in ordered[:3]:
        record = candidate.record if candidate.record is not None else await provider.fetch(candidate.external_id)
        if record is not None:
            return record
    return None


@pytest.mark.parametrize("dir_", [d for d in _manifest_dirs() if not _load(d).get("blocked")], ids=lambda p: p.name)
async def test_the_real_answer_parses_into_the_record_it_stated(dir_: Path, http) -> None:  # type: ignore[no-untyped-def]
    """One corpus entry per provider, asked with the real query, parsed to the
    price/title/rating the capture was recorded as containing."""
    manifest = _load(dir_)
    name = manifest["provider"]
    expected = manifest["expected"]

    with respx.mock(assert_all_called=True) as m:
        _mount(m, manifest)
        record = await _match(_provider(name, http), _query(manifest["corpus_entry"]), manifest["corpus_entry"])
        assert record is not None, f"{name} matched a candidate but fetch() returned nothing"

        for field, value in expected.items():
            got = getattr(record, field)
            if field == "description":
                # Providers truncate; the capture pins presence, not the whole text.
                assert got, f"{name}: expected a description"
            elif isinstance(value, list):
                assert got == value or all(v in (got or []) for v in value), f"{name}: {field} {got!r} != {value!r}"
            else:
                assert got == value, f"{name}: {field} parsed {got!r}, capture said {value!r}"


@pytest.mark.parametrize("dir_", [d for d in _manifest_dirs() if _load(d).get("blocked")], ids=lambda p: p.name)
async def test_a_real_bot_wall_raises_shop_blocked(dir_: Path, http) -> None:  # type: ignore[no-untyped-def]
    """Thalia's 403 was real; the provider must call it blocked, not
    "in stock at no price" and not "this shop has nothing". The ladder keys
    its retry policy off exactly this."""
    manifest = _load(dir_)
    with respx.mock(assert_all_called=True) as m:
        _mount(m, manifest)
        with pytest.raises(ShopBlocked):
            await _provider(manifest["provider"], http).search(_query(manifest["corpus_entry"]))


async def _insert_loan(db, media_key: str) -> int:  # type: ignore[no-untyped-def]
    async with db.write() as w:
        return await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, author, isbn13,
                               first_seen_at, last_seen_at)
            VALUES (?, 'book', 'So geht Technik!', 'Farndon, John',
                    '9783836958424', datetime('now'), datetime('now'))
            """,
            (media_key,),
        )


_SO_GEHT_TECHNIK = MediaQuery(
    media_class=MediaClass.BOOK,
    title="So geht Technik! : Warum Toaster toasten, Flugzeuge fliegen und Wasser aus dem Hahn kommt",
    author="John Farndon",
    isbn="9783836958424",
)


async def test_the_price_ladder_prices_a_real_loan_end_to_end(db, settings, http) -> None:  # type: ignore[no-untyped-def]
    """The whole ladder over captured reality: the shops that stock
    'So geht Technik!' all said 16.00 EUR on their live pages, so the
    effective figure must be the configured-order head at exactly that price,
    with every shop's answer stored for the comparison list."""
    media_id = await _insert_loan(db, "realworld")
    shops = [name for name in PRICE_SHOPS if (REALWORLD / name / "manifest.json").exists()]
    settings.price_providers = shops

    with respx.mock(assert_all_called=False) as m:
        for name in shops:
            _mount(m, _load(REALWORLD / name))
        head = await search_price(db, settings, http, media_id, _SO_GEHT_TECHNIK)

    assert head is not None
    assert head.cents == 1600 and head.basis is PriceBasis.PROVIDER
    found = await all_found_prices(db, settings, media_id)
    assert {name for name, _ in found} == set(shops)
    for _, price in found:
        assert price.cents == 1600


async def test_a_second_ladder_run_asks_nobody_again(db, settings, http) -> None:  # type: ignore[no-untyped-def]
    """The probe table is the no-retry mechanism: once every shop answered,
    walking the corpus again must hit zero extra requests."""
    from bib_tracker.metadata.pricing import search_price as ladder

    media_id = await _insert_loan(db, "realworld-noretry")
    shops = [name for name in PRICE_SHOPS if (REALWORLD / name / "manifest.json").exists()]
    settings.price_providers = shops

    with respx.mock(assert_all_called=False) as m:
        routes = {name: _mount(m, _load(REALWORLD / name)) for name in shops}
        await ladder(db, settings, http, media_id, _SO_GEHT_TECHNIK)
        first = {name: sum(r.calls.call_count for r in rs) for name, rs in routes.items()}
        assert any(first.values()), "the first ladder run asked nothing"
        await ladder(db, settings, http, media_id, _SO_GEHT_TECHNIK)
        second = {name: sum(r.calls.call_count for r in rs) for name, rs in routes.items()}
        assert first == second, f"the second run re-asked: { {n: second[n] - first[n] for n in first} }"
