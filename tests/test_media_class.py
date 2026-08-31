"""Media type classification, driven by the strings the OPACs actually emit."""

from __future__ import annotations

import pytest

from bib_tracker.library.media_class import MediaClass, classify


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Buch", MediaClass.BOOK),
        ("Hörbuch", MediaClass.AUDIOBOOK),
        ("CD", MediaClass.MUSIC),
        ("DVD", MediaClass.MOVIE),
        ("Konventionelles Spiel", MediaClass.GAME),
        ("Zeitschrift", MediaClass.MAGAZINE),
    ],
)
def test_known_library_media_types(raw: str, expected: MediaClass) -> None:
    assert classify(raw) is expected


def test_classification_is_case_insensitive() -> None:
    assert classify("konventionelles spiel") is MediaClass.GAME


@pytest.mark.parametrize(
    ("call_number", "expected"),
    [
        ("S-SPIEL CAT", MediaClass.GAME),
        ("M-CD-K DRE", MediaClass.MUSIC),
        ("M-DVD-S LEB", MediaClass.MOVIE),
    ],
)
def test_call_number_prefix_classifies_when_no_media_type(call_number: str, expected: MediaClass) -> None:
    """Stuttgart puts the type in the call number as well as in the prefix."""
    assert classify(None, call_number=call_number) is expected


def test_media_type_wins_over_the_call_number() -> None:
    assert classify("Buch", call_number="S-SPIEL CAT") is MediaClass.BOOK


def test_a_stuttgart_book_row_has_neither_signal() -> None:
    """Books are the only rows with no bracketed type, so the empty case is a
    book rather than 'other'."""
    assert classify(None, call_number="K-SL SAI") is MediaClass.BOOK


def test_unknown_type_with_an_isbn_is_a_book() -> None:
    assert classify("Sonderbestand", isbn="9783522621885") is MediaClass.BOOK


def test_unknown_type_without_any_hint_is_other() -> None:
    assert classify("Sonderbestand") is MediaClass.OTHER


def test_configured_override_wins() -> None:
    """A new library's vocabulary must be addable without a code change."""
    assert classify("Tiptoi-Stift", overrides={"Tiptoi-Stift": "game"}) is MediaClass.GAME
