"""Cover images, stored in SQLite.

Thumbnails are generated once, on the way in, never on the request path: a
history page shows hundreds of covers, and resizing them per request would
both burn CPU and contend with the poller for the write lock.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import UTC, datetime
from typing import Any

from PIL import Image, ImageOps

from ..db.connection import Database

_LOGGER = logging.getLogger(__name__)

ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
MAX_BYTES = 2 * 1024 * 1024

#: Stored at twice the displayed size, for high-density screens.
VARIANTS: dict[str, tuple[int, int]] = {
    "sm": (128, 192),
    "md": (400, 600),
    "lg": (1000, 1000),
}

#: Placeholder graphics the sources hand out instead of a 404. Storing one
#: would give every unknown title the same wrong cover.
PLACEHOLDER_MARKERS = ("no-image", "no_image", "nocover", "blank")
#: Open Library returns a 1-pixel image when it has no cover for an id.
MIN_DIMENSION = 40


async def has_cover(db: Database, media_id: int) -> bool:
    """Whether a work already has a stored cover.

    The ladders' shared guard: once one source answered, nobody else is asked.
    """
    existing = await db.fetch_one("SELECT cover_sha256 FROM media WHERE id = ?", (media_id,))
    return existing is not None and bool(existing["cover_sha256"])


async def store_cover(db: Database, http: Any, media_id: int, url: str, provider: str = "covers") -> bool:
    """Fetch, thumbnail and store a cover. Returns whether one was stored."""
    if any(marker in url.lower() for marker in PLACEHOLDER_MARKERS):
        return False

    if await has_cover(db, media_id):
        return False

    response = await http.get(url, provider=provider, rate_limit_per_minute=120)
    if not response.ok or not response.body:
        return False
    if len(response.body) > MAX_BYTES:
        _LOGGER.info("Cover for media %d is larger than %d bytes; skipping", media_id, MAX_BYTES)
        return False

    try:
        variants = render_variants(response.body)
    except Exception as err:
        _LOGGER.info("Could not decode cover for media %d: %s", media_id, err)
        return False

    if not variants:
        return False

    digest = hashlib.sha256(response.body).hexdigest()
    now = datetime.now(UTC).isoformat()

    async with db.write() as w:
        for variant, (data, size) in variants.items():
            await w.execute(
                """
                INSERT INTO images (sha256, variant, mime, byte_size, width, height,
                                    source_url, provider, fetched_at, data)
                VALUES (:sha, :variant, 'image/webp', :size, :width, :height,
                        :url, :provider, :now, :data)
                ON CONFLICT (sha256, variant) DO NOTHING
                """,
                {
                    "sha": digest,
                    "variant": variant,
                    "size": len(data),
                    "width": size[0],
                    "height": size[1],
                    "url": url,
                    "provider": provider,
                    "now": now,
                    "data": data,
                },
            )
        aspect = variants["md"][1]
        await w.execute(
            "UPDATE media SET cover_sha256 = ?, cover_aspect = ?, updated_at = datetime('now') WHERE id = ?",
            (digest, f"{aspect[0]}/{aspect[1]}", media_id),
        )
    return True


def render_variants(data: bytes) -> dict[str, tuple[bytes, tuple[int, int]]]:
    """Produce the WebP sizes we serve, preserving aspect ratio."""
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        if image is None:
            return {}
        image = image.convert("RGB")

        rendered: dict[str, tuple[bytes, tuple[int, int]]] = {}
        for variant, box in VARIANTS.items():
            if min(image.size) < MIN_DIMENSION:
                return {}
            copy = image.copy()
            copy.thumbnail(box, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            copy.save(buffer, format="WEBP", quality=80, method=4)
            rendered[variant] = (buffer.getvalue(), copy.size)
        return rendered


async def read_cover(db: Database, media_id: int, variant: str) -> tuple[bytes, str, str] | None:
    """Return (bytes, mime, etag) for a stored cover."""
    row = await db.fetch_one(
        """
        SELECT i.data, i.mime, i.sha256 FROM media m
        JOIN images i ON i.sha256 = m.cover_sha256 AND i.variant = ?
        WHERE m.id = ?
        """,
        (variant, media_id),
    )
    if row is None:
        return None
    return bytes(row["data"]), str(row["mime"]), str(row["sha256"])


def placeholder_svg(title: str, media_class: str) -> str:
    """A generated stand-in, so a missing cover is never a broken image.

    Hue comes from the title, so a shelf of placeholders still looks varied
    rather than like a wall of identical grey boxes.
    """
    hue = int(hashlib.sha256(title.encode()).hexdigest()[:4], 16) % 360
    initials = "".join(word[0] for word in title.split()[:2]).upper() or "?"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300" role="img" '
        f'aria-label="Kein Cover für {_escape(title)}">'
        f'<rect width="200" height="300" fill="hsl({hue} 32% 88%)"/>'
        f'<text x="100" y="150" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="system-ui, sans-serif" font-size="64" font-weight="600" '
        f'fill="hsl({hue} 40% 42%)">{_escape(initials)}</text>'
        f'<text x="100" y="215" text-anchor="middle" font-family="system-ui, sans-serif" '
        f'font-size="16" fill="hsl({hue} 30% 50%)">{_escape(media_class)}</text>'
        "</svg>"
    )


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
