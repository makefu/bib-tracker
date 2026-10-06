"""Ratings are set by clicking stars; the ✕ clear button is gone."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.e2e


def _row(page: Page, title: str) -> Locator:
    return page.locator(f'tr[aria-label^="{title}"]')


def _on_count(row: Locator) -> Locator:
    return row.locator(".rating .star.is-on")


def test_the_clear_button_is_gone_everywhere(app_server: str, page: Page) -> None:
    """Re-rating is the way to change a rating; the ✕ was a second way to do
    the same thing, and it made the widget change width mid-interaction."""
    page.goto(f"{app_server}/history")
    expect(page.locator(".rating__clear")).to_have_count(0)
    page.goto(f"{app_server}/media/1")
    expect(page.locator(".rating__clear")).to_have_count(0)


def test_clicking_a_star_saves_the_rating_without_a_page_reload(app_server: str, page: Page) -> None:
    page.goto(f"{app_server}/history")
    row = _row(page, "Delta")  # seeded unrated
    expect(_on_count(row)).to_have_count(0)

    page.locator('tr[aria-label^="Delta"] .rating .star').nth(3).click()  # the fourth star
    # Alpine fills optimistically and the HTMX swap replaces the widget with
    # the server's answer, so a count of 4 here is a completed round trip.
    expect(_on_count(row)).to_have_count(4)


def test_clicking_different_stars_rerates(app_server: str, page: Page) -> None:
    page.goto(f"{app_server}/history")
    stars = page.locator('tr[aria-label^="Alpha"] .rating .star')
    expect(_on_count(_row(page, "Alpha"))).to_have_count(3)  # seeded at 3

    stars.nth(4).click()
    expect(_on_count(_row(page, "Alpha"))).to_have_count(5)
    stars.nth(1).click()
    expect(_on_count(_row(page, "Alpha"))).to_have_count(2)


def test_the_new_rating_survives_a_reload(app_server: str, page: Page) -> None:
    """Only the POST round trip persists it; this reads it back through a
    fresh render, so an optimistic Alpine state cannot fake the result."""
    page.goto(f"{app_server}/history")
    page.locator('tr[aria-label^="Charlie"] .rating .star').nth(3).click()  # seeded 5 -> 4
    expect(_on_count(_row(page, "Charlie"))).to_have_count(4)

    page.reload()
    expect(_on_count(_row(page, "Charlie"))).to_have_count(4)
