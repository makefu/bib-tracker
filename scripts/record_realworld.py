#!/usr/bin/env python
"""Record verbatim provider responses for the real household loan list.

The corpus is the live bib-tracker instance's own media table (``corpus.json``,
copied from a snapshot of ``/var/lib/bib-tracker/bib-tracker.db`` on bib.lan),
not invented titles: the question these captures answer is whether the
providers really carry what this household actually borrows.

For each provider the corpus is walked in order until the provider produces a
match worth keeping -- a price for the shops and the catalogue, a rating for
BoardGameGeek, a description or cover for the rest. Everything that run
requested is stored verbatim under ``tests/fixtures/realworld/<provider>/``
with a manifest; the offline integration test replays exactly those captures
through the real provider code.

Requires network. Run from the source tree::

    nix develop -c python scripts/record_realworld.py [--provider NAME]...

Credentials come from ``.secrets.yml``, same as the live tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bib_tracker.db.connection import Database  # noqa: E402
from bib_tracker.db.migrator import migrate_path  # noqa: E402
from bib_tracker.library.media_class import MediaClass  # noqa: E402
from bib_tracker.metadata import PROVIDER_FACTORIES, ProviderConfig  # noqa: E402
from bib_tracker.metadata.base import MediaQuery  # noqa: E402
from bib_tracker.metadata.http import CachedClient, CachedResponse  # noqa: E402
from bib_tracker.metadata.shops import ShopBlocked  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "realworld"
SECRETS = [ROOT / ".secrets.yml", Path.home() / "r/ha_stadtbibliothek/.secrets.yml"]

#: What a capture must contain for the provider to count as having matched.
_MUST_HAVE: dict[str, str] = {
    "thalia": "price",
    "buchkatalog": "price",
    "amazon": "price",
    "buch7": "price",
    "lehmanns": "price",
    "ebookde": "price",
    "vlb": "price",
    "dnb": "price",
    "bgg": "rating",
    "googlebooks": "text",
    "openlibrary": "text",
    "wikidata": "text",
}


class RecordingClient(CachedClient):
    """A CachedClient that keeps every response it fetched, verbatim."""

    def __init__(self, db: Database, client: httpx.AsyncClient) -> None:
        super().__init__(db, client)
        self.captured: list[dict[str, Any]] = []

    async def get(  # type: ignore[override]
        self,
        url: str,
        *,
        provider: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        rate_limit_per_minute: int = 60,
        ttl: Any = None,
    ) -> CachedResponse:
        response = await super().get(
            url,
            provider=provider,
            params=params,
            headers=headers,
            rate_limit_per_minute=rate_limit_per_minute,
            ttl=ttl,
        )
        self.captured.append(
            {"method": "GET", "url": url, "params": params, "status": response.status, "body": response.body}
        )
        return response

    async def post_json(  # type: ignore[override]
        self,
        url: str,
        *,
        provider: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        rate_limit_per_minute: int = 60,
        ttl: Any = None,
    ) -> CachedResponse:
        response = await super().post_json(
            url,
            provider=provider,
            payload=payload,
            params=params,
            headers=headers,
            rate_limit_per_minute=rate_limit_per_minute,
            ttl=ttl,
        )
        self.captured.append(
            {
                "method": "POST",
                "url": url,
                "params": params,
                "payload": payload,
                "status": response.status,
                "body": response.body,
            }
        )
        return response


def _secrets() -> dict[str, Any]:
    for path in SECRETS:
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    return {}


def _api_key(secrets: dict[str, Any], provider: str) -> str | None:
    inline = secrets.get("metadata_api_keys")
    if isinstance(inline, dict) and isinstance(inline.get(provider), str):
        return str(inline[provider])
    files = secrets.get("metadata_api_key_files")
    if isinstance(files, dict) and isinstance(files.get(provider), str):
        try:
            return (ROOT / str(files[provider])).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _slug(url: str, params: dict[str, Any] | None, status: int, body: bytes) -> str:
    path = re.sub(r"[^a-zA-Z0-9]+", "_", url.split("://", 1)[-1].split("/", 1)[-1]).strip("_").lower()[:48] or "root"
    tail = "json" if body[:1] in (b"{", b"[") else "xml" if body[:5] == b"<?xml" else "html"
    term = ""
    if params:
        for key in ("sq", "k", "q", "search", "rpp", "term"):
            if key in params:
                term = f"-{re.sub(r'[^a-zA-Z0-9]+', '_', str(params[key])).strip('_').lower()[:40]}"
                break
    return f"{path}{term}-{status}.{tail}"


def _manifest(
    provider: str,
    entry: dict[str, Any],
    captures: list[dict[str, Any]],
    expected: dict[str, Any],
    blocked: str | None,
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """(manifest, [(filename, body)]) with duplicate URLs collapsed.

    The search fallback re-asks a term the ISBN search already missed; storing
    that response twice would double the corpus for nothing.
    """
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for cap in captures:
        key = (cap["method"], cap["url"], json.dumps(cap.get("params"), sort_keys=True))
        seen.setdefault(key, cap)
    manifest: dict[str, Any] = {
        "provider": provider,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus_entry": entry,
        "captures": [],
        "expected": expected,
    }
    if blocked is not None:
        manifest["blocked"] = blocked
    files: list[tuple[str, bytes]] = []
    for index, cap in enumerate(seen.values()):
        name = f"{index:02d}-{_slug(cap['url'], cap.get('params'), cap['status'], cap['body'])}"
        files.append((name, cap["body"]))
        manifest["captures"].append(
            {
                "file": name,
                "method": cap["method"],
                "url": cap["url"],
                "params": cap.get("params"),
                "payload": cap.get("payload"),
                "status": cap["status"],
            }
        )
    return manifest, files


def _expected(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    keep = {
        "external_id": record.external_id,
        "title": record.title,
        "authors": record.authors,
        "isbn13": record.isbn13,
        "publisher": record.publisher,
        "published_year": record.published_year,
        "description": record.description,
        "list_price_cents": record.list_price_cents,
        "list_price_currency": record.list_price_currency,
        "rating_value": record.rating_value,
        "rating_scale": record.rating_scale,
        "cover_source_url": record.cover_source_url,
    }
    return {k: v for k, v in keep.items() if v not in (None, [], "")}


def _has(need: str, record: Any) -> bool:
    if record is None:
        return False
    if need == "price":
        return record.list_price_cents is not None
    if need == "rating":
        return record.rating_value is not None
    return bool(record.description or record.cover_source_url or record.title)


def _query(entry: dict[str, Any]) -> MediaQuery:
    return MediaQuery(
        media_class=MediaClass(entry["class"]),
        title=entry["title"],
        author=entry.get("author"),
        isbn=entry.get("isbn"),
    )


async def _match_for(provider: Any, entry: dict[str, Any], need: str) -> tuple[Any, Any] | None:
    """(candidate, record) if the provider has something worth keeping for this entry."""
    candidates = await provider.search(_query(entry))
    # Prefer the candidate whose ISBN is the one we asked for; the shops mix
    # formats and editions into a results page.
    asked = entry.get("isbn")
    ordered = sorted(candidates, key=lambda c: 0 if asked and c.isbn13 == asked else 1)
    for candidate in ordered[:3]:
        record = (
            candidate.record
            if getattr(candidate, "record", None) is not None
            else await provider.fetch(candidate.external_id)
        )
        if _has(need, record):
            return candidate, record
    return None


async def record(providers: list[str]) -> None:
    corpus = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    secrets = _secrets()
    # ISBN entries first: an exact-match capture is the strongest evidence a
    # parser can have, and every shop stocks the same five.
    corpus = sorted(corpus, key=lambda e: 0 if e.get("isbn") else 1)

    for name in providers:
        factory = PROVIDER_FACTORIES[name]
        api_key = _api_key(secrets, name)
        config = ProviderConfig(name=name, api_key=api_key, rate_limit_per_minute=6000)
        provider = factory(config, None)  # type: ignore[call-arg]
        if not provider.available():
            print(f"{name:12s} skipped (no credential)")
            continue

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as raw:
            if name == "amazon":
                # Warm the cookie jar; a cold amazon.de answers scripted first
                # requests with the JS challenge page.
                await raw.get("https://www.amazon.de/")
            entry: dict[str, Any] | None = None
            outcome: tuple[Any, Any] | None = None
            blocked: str | None = None
            captured: list[dict[str, Any]] = []
            for candidate_entry in corpus:
                db_path = Path(tempfile.mkdtemp()) / "r.db"
                migrate_path(db_path)
                db = Database(db_path)
                http = RecordingClient(db, raw)
                provider.client = http
                try:
                    outcome = await _match_for(provider, candidate_entry, _MUST_HAVE[name])
                except ShopBlocked as err:
                    blocked = str(err)
                    outcome = None
                except Exception as err:  # keep recording the other shops
                    print(f"{name:12s} error on {candidate_entry['title'][:30]!r}: {err}")
                captured = list(http.captured)
                if outcome is not None:
                    entry = candidate_entry
                    blocked = None
                    break
                # A hard bot wall will not lift by changing the book.
                if blocked is not None:
                    break

        provider_dir = FIXTURES / name
        if entry is None or outcome is None:
            # A bot wall is real-world state: store what the shop actually
            # answered so the replay test can pin the ShopBlocked path.
            if blocked is not None and captured:
                provider_dir.mkdir(parents=True, exist_ok=True)
                manifest, files = _manifest(name, candidate_entry, captured, {}, blocked)
                for filename, body in files:
                    (provider_dir / filename).write_bytes(body)
                (provider_dir / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
                )
            print(f"{name:12s} NO MATCH{' (blocked: ' + str(blocked) + ')' if blocked else ''}")
            continue
        _, record_obj = outcome
        provider_dir.mkdir(parents=True, exist_ok=True)
        manifest, files = _manifest(name, entry, captured, _expected(record_obj), blocked)
        for filename, body in files:
            (provider_dir / filename).write_bytes(body)
        (provider_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        price = getattr(record_obj, "list_price_cents", None)
        print(
            f"{name:12s} matched {entry['isbn'] or entry['title'][:40]!r}"
            + (f" @ {price / 100:.2f} EUR" if price else "")
            + f" -> {provider_dir.relative_to(ROOT)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", dest="providers")
    args = parser.parse_args()
    providers = args.providers or list(PROVIDER_FACTORIES)
    asyncio.run(record(providers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
