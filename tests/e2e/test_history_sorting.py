"""Clicking a history column header sorts the table both ways."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

# The seeded rows and their orders. Every expectation is written out rather
# than computed from the seed data: a test that derives its own expectation
# from the same expression as the code proves nothing about NULL placement
# or the direction flip.
CASES = [
    pytest.param("title", ["Alpha", "Bravo", "Charlie", "Delta"], ["Delta", "Charlie", "Bravo", "Alpha"], id="title"),
    pytest.param("status", ["Alpha", "Charlie", "Delta", "Bravo"], ["Delta", "Bravo", "Alpha", "Charlie"], id="status"),
    pytest.param("due", ["Delta", "Bravo", "Alpha", "Charlie"], ["Charlie", "Alpha", "Bravo", "Delta"], id="due"),
    pytest.param("lend", ["Bravo", "Delta", "Alpha", "Charlie"], ["Charlie", "Alpha", "Delta", "Bravo"], id="lend"),
    pytest.param("return", ["Delta", "Bravo", "Alpha", "Charlie"], ["Bravo", "Delta", "Alpha", "Charlie"], id="return"),
    pytest.param(
        "renewals",
        ["Bravo", "Charlie", "Alpha", "Delta"],
        ["Delta", "Alpha", "Bravo", "Charlie"],
        id="renewals",
    ),
    pytest.param("price", ["Alpha", "Charlie", "Bravo", "Delta"], ["Bravo", "Charlie", "Alpha", "Delta"], id="price"),
    pytest.param("rating", ["Alpha", "Charlie", "Bravo", "Delta"], ["Charlie", "Alpha", "Bravo", "Delta"], id="rating"),
]


def _sort_link(page: Page, key: str):
    return page.locator(f'th[data-sort="{key}"] a')


def test_the_default_order_is_newest_loan_first(app_server: str, page: Page, titles) -> None:
    page.goto(f"{app_server}/history")
    # Lend dates: Charlie 01-12, Alpha 01-10, Delta 01-08, Bravo 01-05.
    assert titles() == ["Charlie", "Alpha", "Delta", "Bravo"]
    # No column claims the sort until one is clicked.
    assert page.locator('th[aria-sort="ascending"], th[aria-sort="descending"]').count() == 0


@pytest.mark.parametrize(("key", "ascending", "descending"), CASES)
def test_clicking_a_header_sorts_ascending_then_descending(
    app_server: str,
    page: Page,
    titles,
    key: str,
    ascending: list[str],
    descending: list[str],
) -> None:
    page.goto(f"{app_server}/history")

    _sort_link(page, key).click()
    page.wait_for_url(f"{app_server}/history?sort={key}&dir=asc")
    assert titles() == ascending
    assert page.locator(f'th[data-sort="{key}"]').get_attribute("aria-sort") == "ascending"

    _sort_link(page, key).click()
    page.wait_for_url(f"{app_server}/history?sort={key}&dir=desc")
    assert titles() == descending
    assert page.locator(f'th[data-sort="{key}"]').get_attribute("aria-sort") == "descending"


def test_sorting_survives_a_direct_visit(app_server: str, page: Page, titles) -> None:
    """The sort lives in the URL, so a bookmark (or a no-JS click) shows the
    same order the browser put there."""
    page.goto(f"{app_server}/history?sort=price&dir=desc")
    assert titles() == ["Bravo", "Charlie", "Alpha", "Delta"]
    assert page.locator('th[data-sort="price"]').get_attribute("aria-sort") == "descending"


def test_an_unknown_sort_falls_back_to_the_default(app_server: str, page: Page, titles) -> None:
    """?sort is user-editable; it must never reach the SQL unvalidated."""
    page.goto(f"{app_server}/history?sort=;drop&dir=sideways")
    assert titles() == ["Charlie", "Alpha", "Delta", "Bravo"]
