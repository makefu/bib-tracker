"""Copy and work fingerprints."""

from __future__ import annotations

from bib_tracker.library.identity import author_key, copy_key, media_key, normalise
from bib_tracker.library.media_class import MediaClass


def _stuttgart_book() -> dict[str, str]:
    """The book row from ha_stadtbibliothek's stuttgart_ausleihen.html."""
    return {
        "title": "Der kleine Prinz",
        "author": "Saint-Exupéry, Antoine de",
        "call_number": "K-SL SAI",
        "barcode": "12345678",
        "item_id": "12345678",
    }


def _stuttgart_game() -> dict[str, str]:
    """The board game row: no barcode exists for Stuttgart media."""
    return {
        "title": "Catan - Das Spiel",
        "call_number": "S-SPIEL CAT",
        "item_id": "S-SPIEL CAT",
        "media_type": "Konventionelles Spiel",
    }


def test_normalise_folds_diacritics_sort_markers_and_punctuation() -> None:
    assert normalise("¬Der¬ kleine Prinz!") == "der kleine prinz"
    assert normalise("Saint-Exupéry, Antoine de") == "saint exupery antoine de"


def test_copy_key_is_stable_across_polls() -> None:
    assert copy_key("stuttgart", **_stuttgart_book()) == copy_key("stuttgart", **_stuttgart_book())


def test_copy_key_prefers_the_barcode() -> None:
    """A retitled or recatalogued record must stay the same exemplar."""
    renamed = _stuttgart_book() | {"title": "Der Kleine Prinz (Neuausgabe)", "call_number": "K-SL EXU"}
    assert copy_key("stuttgart", **renamed) == copy_key("stuttgart", **_stuttgart_book())


def test_copy_key_for_media_without_a_barcode_uses_call_number_and_title() -> None:
    """The call number alone is a shelf class: two different games can share
    it, so the title has to be part of the key."""
    game = _stuttgart_game()
    other = game | {"title": "Catan - Seefahrer"}
    assert copy_key("stuttgart", **game) != copy_key("stuttgart", **other)


def test_copy_key_differs_between_libraries() -> None:
    """Two libraries may hand out the same itemnumber for different things."""
    args = {"title": "Momo", "item_id": "500002"}
    assert copy_key("remseck", **args) != copy_key("stuttgart", **args)


def test_remseck_copy_key_uses_the_itemnumber() -> None:
    base = {"title": "Momo", "item_id": "500002"}
    assert copy_key("remseck", **base) == copy_key("remseck", **(base | {"call_number": "End"}))


def test_stuttgart_item_id_is_not_used_as_an_itemnumber() -> None:
    """Stuttgart's item_id is the call number for media, which is not an
    exemplar identifier -- the ladder must not treat it as one."""
    game = _stuttgart_game()
    without_call_number = {k: v for k, v in game.items() if k != "call_number"}
    assert copy_key("stuttgart", **game) != copy_key("stuttgart", **without_call_number)


def test_author_key_agrees_across_name_orders_and_role_annotations() -> None:
    assert author_key("Kling, Marc-Uwe") == author_key("Marc-Uwe Kling")
    assert author_key("Kling, Marc-Uwe [Verfasser]") == author_key("Kling, Marc-Uwe")


def test_media_key_groups_repeat_borrows_of_one_work() -> None:
    first = media_key(MediaClass.BOOK, "¬Die¬ unendliche Geschichte", "Ende, Michael")
    second = media_key(MediaClass.BOOK, "Die unendliche Geschichte", "Michael Ende")
    assert first == second


def test_media_key_separates_a_book_from_its_film() -> None:
    args = ("Die unendliche Geschichte", "Ende, Michael")
    assert media_key(MediaClass.BOOK, *args) != media_key(MediaClass.MOVIE, *args)
