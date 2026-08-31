"""Cover fetching and thumbnailing."""

from __future__ import annotations

import io

import httpx
import pytest
import respx
from PIL import Image

from bib_tracker.metadata.covers import VARIANTS, placeholder_svg, read_cover, render_variants, store_cover
from bib_tracker.metadata.http import CachedClient


def png(width: int, height: int, colour: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
async def http(db):
    async with httpx.AsyncClient() as client:
        yield CachedClient(db, client)


@pytest.fixture
async def media_id(db):
    async with db.write() as w:
        return await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, first_seen_at, last_seen_at)
            VALUES ('k', 'book', 'Momo', datetime('now'), datetime('now'))
            """
        )


def test_variants_preserve_the_aspect_ratio() -> None:
    rendered = render_variants(png(800, 1200))

    assert set(rendered) == set(VARIANTS)
    for _variant, (data, size) in rendered.items():
        assert data.startswith(b"RIFF")  # WebP
        assert abs(size[0] / size[1] - 800 / 1200) < 0.02


def test_a_tiny_image_is_rejected() -> None:
    """Open Library answers with a 1-pixel image when it has no cover, and
    storing it would give the work a blank square forever."""
    assert render_variants(png(1, 1)) == {}


@respx.mock
async def test_storing_a_cover_makes_every_variant_available(db, http, media_id) -> None:
    respx.get("http://covers.test/x.jpg").mock(return_value=httpx.Response(200, content=png(600, 900)))

    assert await store_cover(db, http, media_id, "http://covers.test/x.jpg") is True

    for variant in VARIANTS:
        stored = await read_cover(db, media_id, variant)
        assert stored is not None
        data, mime, etag = stored
        assert mime == "image/webp"
        assert len(data) > 0
        assert len(etag) == 64

    row = await db.fetch_one("SELECT cover_sha256, cover_aspect FROM media WHERE id = ?", (media_id,))
    assert row["cover_sha256"]
    assert "/" in row["cover_aspect"]


@respx.mock
async def test_a_placeholder_url_is_never_fetched(db, http, media_id) -> None:
    """Koha links a "no-image" graphic rather than omitting the tag."""
    route = respx.get("http://covers.test/no-image.png").mock(return_value=httpx.Response(200, content=png(600, 900)))

    assert await store_cover(db, http, media_id, "http://covers.test/no-image.png") is False
    assert not route.called


@respx.mock
async def test_an_oversized_image_is_skipped(db, http, media_id) -> None:
    respx.get("http://covers.test/huge.jpg").mock(return_value=httpx.Response(200, content=b"x" * (3 * 1024 * 1024)))

    assert await store_cover(db, http, media_id, "http://covers.test/huge.jpg") is False


@respx.mock
async def test_a_broken_image_is_skipped_without_raising(db, http, media_id) -> None:
    respx.get("http://covers.test/broken.jpg").mock(return_value=httpx.Response(200, content=b"not an image"))

    assert await store_cover(db, http, media_id, "http://covers.test/broken.jpg") is False


@respx.mock
async def test_identical_covers_are_stored_once(db, http, media_id) -> None:
    """Editions share art, and the same bytes should not fill the database."""
    image = png(600, 900)
    respx.get("http://covers.test/a.jpg").mock(return_value=httpx.Response(200, content=image))
    respx.get("http://covers.test/b.jpg").mock(return_value=httpx.Response(200, content=image))

    async with db.write() as w:
        second = await w.execute(
            """
            INSERT INTO media (media_key, media_class, title, first_seen_at, last_seen_at)
            VALUES ('k2', 'book', 'Momo (Neuausgabe)', datetime('now'), datetime('now'))
            """
        )

    await store_cover(db, http, media_id, "http://covers.test/a.jpg")
    await store_cover(db, http, second, "http://covers.test/b.jpg")

    rows = await db.fetch_all("SELECT DISTINCT sha256 FROM images")
    assert len(rows) == 1


def test_the_placeholder_is_valid_svg_and_varies_by_title() -> None:
    one = placeholder_svg("Die unendliche Geschichte", "Buch")
    two = placeholder_svg("Momo", "Buch")

    assert one.startswith("<svg") and one.endswith("</svg>")
    assert 'role="img"' in one
    assert "DU" in one
    # A shelf of placeholders should not be a wall of identical grey boxes.
    assert one != two


def test_the_placeholder_escapes_its_title() -> None:
    svg = placeholder_svg('<script>alert("x")</script>', "Buch")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
