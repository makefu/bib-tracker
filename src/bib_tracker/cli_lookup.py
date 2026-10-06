"""`bib-tracker-lookup`: ask the providers directly, without the database.

The same provider code the price and cover ladders walk, driven from a
terminal: one ISBN (or title), every configured source answered once, the
findings printed side by side. Useful for deciding whether a shop is broken,
blocked, or simply does not stock the work — the question the UI's probe
markers raise but cannot answer in detail.

Answers are cached in the XDG cache directory rather than the application
database: this tool reads shops, it does not enrich anything. `--fresh`
empties that cache before asking.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "bib-tracker" / "lookup-cache.db"


def _lookup_providers(settings: Any, wanted: list[str], all_providers: bool) -> list[str]:
    """Provider names to ask, in order.

    Without --provider the walk follows the configured price ladder and then
    the remaining catalogue sources, so one run shows every answer the app
    could combine.
    """
    from .metadata import PROVIDER_FACTORIES

    if all_providers:
        return list(PROVIDER_FACTORIES)
    if wanted:
        unknown = [name for name in wanted if name not in PROVIDER_FACTORIES]
        if unknown:
            raise KeyError(f"Unknown provider(s): {', '.join(unknown)}")
        return wanted
    order = [name for name in settings.price_providers if name in PROVIDER_FACTORIES]
    order += [name for name in PROVIDER_FACTORIES if name not in order]
    return order


async def _lookup(args: argparse.Namespace, settings: Any) -> tuple[list[dict[str, Any]], int]:
    import httpx

    from .db.connection import Database
    from .db.migrator import migrate_path
    from .library.media_class import MediaClass
    from .metadata import PROVIDER_FACTORIES
    from .metadata.base import MediaQuery, ProviderConfig
    from .metadata.http import CachedClient
    from .metadata.matcher import rank

    cache_path = _cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    migrate_path(cache_path)
    if args.fresh:
        with sqlite3.connect(cache_path) as conn:
            conn.execute("DELETE FROM http_cache")

    cache_db = Database(cache_path)
    rows: list[dict[str, Any]] = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0)) as raw:
        http = CachedClient(cache_db, raw, contact=settings.user_agent_contact)

        def make(name: str) -> Any:
            factory = PROVIDER_FACTORIES[name]
            return factory(
                ProviderConfig(
                    name=name,
                    base_url=settings.metadata_base_urls.get(name),
                    api_key=settings.provider_api_key(name),
                    cookie=settings.provider_cookie(name),
                    rate_limit_per_minute=settings.provider_rate_limit(name),
                ),
                http,
            )

        names = _lookup_providers(settings, args.provider, args.all_providers)
        providers: dict[str, Any] = {}
        skipped: list[str] = []
        for name in names:
            provider = make(name)
            if provider.available():
                providers[name] = provider
            else:
                skipped.append(name)

        isbn = args.isbn
        title = args.title
        if isbn and not title and providers:
            # The shops' ISBN searches fall back to title+author when the
            # ISBN finds nothing (Thalia returns zero tiles for a plain ISBN
            # query), so the resolver's title is what makes that fallback
            # work here too.
            from .metadata.lookup import lookup_isbn

            class _Source:
                def usable_providers(self) -> dict[str, Any]:
                    return providers

            record = await lookup_isbn(_Source(), isbn)
            if record is not None:
                title = record.title

        query = MediaQuery(
            media_class=MediaClass.BOOK if isbn or title else MediaClass.OTHER,
            title=title or (isbn or ""),
            author=args.author,
            isbn=isbn,
        )

        for name in names:
            provider = providers.get(name)
            if provider is None:
                continue
            entry: dict[str, Any] = {"provider": name}
            try:
                candidates = await provider.search(query)
                ranked = rank(query, candidates)
                for cand in ranked[: args.limit]:
                    record = cand.record
                    if record is None:
                        record = await provider.fetch(cand.external_id)
                    entry = {
                        "provider": name,
                        "external_id": cand.external_id,
                        "external_url": getattr(record, "external_url", None),
                        "title": (record.title if record and record.title else cand.title),
                        "match_score": cand.score,
                        "price_cents": record.list_price_cents if record else None,
                        "price_currency": record.list_price_currency if record else None,
                        "description": record.description if record else None,
                        "cover_url": record.cover_source_url if record else None,
                    }
                    break
                if not candidates:
                    entry["note"] = "keine Treffer"
            except Exception as err:
                entry["note"] = f"Fehler: {err}"
            rows.append(entry)

        if skipped and not args.json:
            print(f"übersprungen (Zugangsdaten fehlen): {', '.join(skipped)}", file=sys.stderr)

    cache_db.close()
    answered = any(row.get("price_cents") is not None or row.get("description") or row.get("cover_url") for row in rows)
    return rows, 0 if answered else 2


def _print_human(rows: list[dict[str, Any]]) -> None:
    from .web.filters import provider_label

    for row in rows:
        label = provider_label(row["provider"])
        score = row.get("match_score")
        head = f"{label}" + (f"  (Treffer {score:.2f})" if isinstance(score, int | float) else "")
        print(head)
        print(f"  Titel:  {row.get('title') or '—'}")
        cents = row.get("price_cents")
        if cents is not None:
            amount = f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            print(f"  Preis:  {amount} {row.get('price_currency') or 'EUR'}")
        else:
            print("  Preis:  —")
        cover = row.get("cover_url")
        print(f"  Bild:   {cover or '—'}")
        desc = row.get("description")
        one_line = " ".join(str(desc).split()) if desc else ""
        print(f"  Text:   {one_line[:200] or '—'}")
        if row.get("note"):
            print(f"  Hinweis: {row['note']}")
        print()


def lookup(argv: list[str] | None = None) -> int:
    """Query every configured price/image source for one work and print the answers."""
    parser = argparse.ArgumentParser(
        prog="bib-tracker-lookup",
        description="Look up price, cover and description for one work from the configured providers",
    )
    parser.add_argument("isbn", nargs="?", help="ISBN-10 or ISBN-13 of the work")
    parser.add_argument("--title", help="Title to search (resolved from the ISBN when omitted)")
    parser.add_argument("--author", help="Author to disambiguate the title search")
    parser.add_argument(
        "--provider",
        action="append",
        dest="provider",
        metavar="NAME",
        help="Ask only this provider (repeatable, order decides preference)",
    )
    parser.add_argument(
        "--all-providers",
        action="store_true",
        help="Ask every known provider, not just configured ones",
    )
    parser.add_argument("--fresh", action="store_true", help="Empty the lookup HTTP cache before asking")
    parser.add_argument("--json", action="store_true", dest="json", help="Print the answers as a JSON array")
    parser.add_argument("--limit", type=int, default=1, metavar="N", help="Show up to N candidates per provider")
    parser.add_argument("--config-file", action="append", type=Path, default=None, metavar="FILE")
    parser.add_argument("--version", action="version", version=f"bib-tracker {__version__}")
    args = parser.parse_args(argv)

    if not args.isbn and not args.title:
        parser.error("give an ISBN or --title")

    from .cli import _configure_logging, _load_settings_or_exit

    settings, status = _load_settings_or_exit(args.config_file)
    if status or settings is None:
        return status
    _configure_logging(settings.log_level)

    try:
        rows, code = asyncio.run(_lookup(args, settings))
    except KeyError as err:
        print(str(err).strip("'\""), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        _print_human(rows)
    return code


if __name__ == "__main__":
    sys.exit(lookup())
