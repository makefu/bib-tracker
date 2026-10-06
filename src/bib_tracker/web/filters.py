"""Jinja filters. German formatting throughout, since that is what the
libraries and the household use."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from jinja2 import Environment

from ..library.media_class import MediaClass

#: Typographic characters, spelled out so they cannot be mistaken for the
#: ASCII lookalikes a linter (rightly) warns about.
EN_DASH = "\u2013"  # stands in for "no value"
NBSP = "\u00a0"  # keeps "12,50 €" from breaking across a line

MEDIA_CLASS_LABELS: dict[str, str] = {
    MediaClass.BOOK: "Buch",
    MediaClass.AUDIOBOOK: "Hörbuch",
    MediaClass.MUSIC: "CD",
    MediaClass.MOVIE: "Film",
    MediaClass.GAME: "Spiel",
    MediaClass.MAGAZINE: "Zeitschrift",
    MediaClass.OTHER: "Sonstiges",
}

MEDIA_CLASS_ICONS: dict[str, str] = {
    MediaClass.BOOK: "📕",
    MediaClass.AUDIOBOOK: "🎧",
    MediaClass.MUSIC: "💿",
    MediaClass.MOVIE: "🎬",
    MediaClass.GAME: "🎲",
    MediaClass.MAGAZINE: "📰",
    MediaClass.OTHER: "📦",
}


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def de_date(value: Any) -> str:
    parsed = _as_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else EN_DASH


def de_date_short(value: Any) -> str:
    parsed = _as_date(value)
    if parsed is None:
        return EN_DASH
    if parsed.year == date.today().year:
        return parsed.strftime("%d.%m.")
    return parsed.strftime("%d.%m.%y")


def de_num(value: Any) -> str:
    if value is None:
        return EN_DASH
    return f"{value:,}".replace(",", ".")


def eur(cents: Any) -> str:
    if cents is None:
        return EN_DASH
    # German convention: 1.534,50 with a non-breaking space before the sign.
    formatted = f"{cents / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{formatted}{NBSP}\u20ac"


def rel_days(value: Any) -> str:
    """ "in 4 Tagen" / "seit 3 Tagen überfällig" -- the thing you actually want
    to know about a due date."""
    parsed = _as_date(value)
    if parsed is None:
        return ""
    delta = (parsed - date.today()).days
    if delta == 0:
        return "heute fällig"
    if delta == 1:
        return "morgen fällig"
    if delta > 1:
        return f"fällig in {delta} Tagen"
    if delta == -1:
        return "1 Tag überfällig"
    return f"{abs(delta)} Tage überfällig"


def duration_de(days: Any) -> str:
    if days is None:
        return EN_DASH
    days = int(days)
    if days == 1:
        return "1 Tag"
    return f"{days} Tage"


def filesizeformat_de(value: Any) -> str:
    """Bytes as a German would write them: 1,4 MB with a comma."""
    if value is None:
        return EN_DASH
    n = float(value)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            formatted = f"{n:.1f}".rstrip("0").rstrip(".") if unit != "B" else str(int(n))
            formatted = formatted.replace(".", ",")
            return f"{formatted}{NBSP}{unit}"
        n /= 1000.0
    return EN_DASH  # pragma: no cover - the loop always returns


#: How each price basis is explained to a person, wording identical to the
#: media page's inline chain so both say the same thing.
_BASIS_LABELS: dict[str, str] = {
    "manual": "von dir eingetragen",
    "provider_list_price": "Listenpreis",
    "default_by_class": "gesetzt (klassen-Standardpreis)",
    "unknown": "unbekannt",
}

#: How shop and catalogue providers are named to a person, keyed by provider
#: name. Anything unlisted shows its raw name rather than a placeholder.
PROVIDER_LABELS: dict[str, str] = {
    "buchkatalog": "Buchkatalog.de",
    "thalia": "Thalia",
    "amazon": "Amazon.de",
    "buch7": "buch7.de",
    "lehmanns": "Lehmanns.de",
    "ebookde": "eBook.de",
    "vlb": "VLB",
    "dnb": "Deutsche Nationalbibliothek",
    "googlebooks": "Google Books",
    "openlibrary": "Open Library",
    "wikidata": "Wikidata",
    "bgg": "BoardGameGeek",
    "library": "Bibliothek",
    "legacy": "Eintrag aus früherer Version",
}


def provider_label(value: Any) -> str:
    return PROVIDER_LABELS.get(str(value), str(value or ""))


def price_provider_label(basis: Any, provider: Any) -> str:
    """One label for every place a price is explained, so the cell and the
    media page agree — and both name the source, not just the basis word."""
    key = str(basis)
    if key == "manual":
        return "von dir eingetragen"
    if key == "provider_list_price":
        return f"Listenpreis von {PROVIDER_LABELS.get(str(provider), str(provider or 'Provider'))}"
    if key == "default_by_class":
        return "gesetzt (klassen-Standardpreis)"
    return "unbekannt"


def price_basis_label(basis: Any) -> str:
    return _BASIS_LABELS.get(str(basis), "unbekannt")


def renew_tone(loan: dict[str, Any]) -> str:
    """The row colour for the due-soon list: red when the library will refuse,
    orange for the last renewal, yellow for the next-to-last. Colour never
    carries the meaning alone -- the count and the disabled button say it too.
    """
    if not loan["can_be_renewed"]:
        return "none"
    maximum = loan["max_renewals"]
    if maximum is None:
        return ""
    left = maximum - (loan["times_renewed"] or 0)
    if left <= 0:
        return "none"
    if left == 1:
        return "last"
    if left == 2:
        return "two"
    return ""


def media_label(media_class: Any) -> str:
    return MEDIA_CLASS_LABELS.get(str(media_class), "Sonstiges")


def media_icon(media_class: Any) -> str:
    return MEDIA_CLASS_ICONS.get(str(media_class), "📦")


def register_filters(env: Environment) -> None:
    env.filters.update(
        de_date=de_date,
        de_date_short=de_date_short,
        de_num=de_num,
        eur=eur,
        rel_days=rel_days,
        duration_de=duration_de,
        media_label=media_label,
        media_icon=media_icon,
        price_basis_label=price_basis_label,
        price_provider_label=price_provider_label,
        provider_label=provider_label,
        renew_tone=renew_tone,
        filesizeformat_de=filesizeformat_de,
    )
