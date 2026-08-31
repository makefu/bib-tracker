"""Stable identifiers for physical copies and for works.

Polls have no shared identifier to join on beyond what the OPAC prints, and
what it prints differs by library and by media type -- Stuttgart's item_id is
an exemplar barcode for books but a shelf mark for CDs and games. The ladder
below makes that heterogeneity explicit rather than accidental.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .media_class import MediaClass

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise(value: str | None) -> str:
    """Fold a catalogue string down to something comparable across polls."""
    if not value:
        return ""
    text = value.replace("¬", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCTUATION.sub(" ", text.casefold())
    return _WHITESPACE.sub(" ", text).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def copy_key(
    library_type: str,
    *,
    title: str,
    item_id: str | None = None,
    barcode: str | None = None,
    call_number: str | None = None,
    author: str | None = None,
    media_type: str | None = None,
) -> str:
    """Identify one physical exemplar within one account.

    Preference order, most to least stable:

    1. an exemplar barcode, which Stuttgart prints for books;
    2. a Koha itemnumber, which is exemplar-stable;
    3. call number plus title -- Stuttgart's media rows carry no barcode, and
       the call number alone identifies a shelf class, not an item;
    4. title, author and media type, when the OPAC gave us nothing else.
    """
    if barcode:
        base = f"barcode:{barcode.strip()}"
    elif library_type == "remseck" and item_id:
        base = f"item:{item_id.strip()}"
    elif call_number:
        base = f"callno:{normalise(call_number)}|t:{normalise(title)}"
    else:
        base = f"t:{normalise(title)}|a:{normalise(author)}|m:{normalise(media_type)}"
    return _digest(f"{library_type}|{base}")


def author_key(author: str | None) -> str:
    """Fold an author string so "Kling, Marc-Uwe" and "Marc-Uwe Kling" agree.

    Catalogue author strings are dirty: role annotations, inconsistent name
    order, stray punctuation. Only the surname is reliable enough to group on.
    """
    if not author:
        return ""
    text = re.sub(r"\[[^\]]*\]", " ", author)
    text = re.sub(r"\b(hrsg|verf|verfasser|mitwirkender|übers|illustr)\.?\b", " ", text, flags=re.IGNORECASE)
    if "," in text:
        surname = text.split(",", 1)[0]
    else:
        parts = normalise(text).split()
        surname = parts[-1] if parts else ""
    return normalise(surname)


def media_key(media_class: MediaClass | str, title: str, author: str | None = None) -> str:
    """Identify a work across accounts and across libraries."""
    return _digest(f"{MediaClass(media_class).value}|{normalise(title)}|{author_key(author)}")
