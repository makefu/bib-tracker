"""The identify dialog is an EventSource stream plus a fetch-driven apply;
neither runs in the ASGI client. The browser drives the real round trip
against a local fake shop serving the recorded Buchkatalog fixtures."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_the_dialog_streams_the_cards_and_adopting_one_rewrites_the_row(app_server: str, page: Page) -> None:
    """Media 5 is 'Epsilon' with the ISBN of the recorded Unterwerfung edition:
    the stream's ISBN lookup answers with exactly that one product, and
    applying it must replace the row's title (a hand-picked record overrides
    the OPAC's naming) and re-render the page under the same id."""
    page.goto(f"{app_server}/media/5")
    page.click("#identify-open")

    dialog = page.locator("#identify-dialog")
    expect(dialog).to_be_visible()

    # One configured source, and it reports what it found rather than
    # sitting in 'searching'.
    expect(page.locator("#identify-providers .identify__provider")).to_have_count(1)
    expect(page.locator("#identify-providers .identify__chip")).to_have_attribute("data-status", "found")
    expect(page.locator("#identify-providers .identify__status")).to_have_text("1 Treffer")

    card = dialog.locator(".identify-card")
    expect(card).to_have_count(1)
    expect(card).to_have_attribute("data-provider", "buchkatalog")
    expect(card).to_have_attribute("data-external-id", "4099276460851090106")
    expect(card).to_have_attribute("data-isbn13", "9783832163594")
    expect(card).to_contain_text("Unterwerfung")
    expect(card).to_contain_text("15,00")
    # The card's cover loads from the fake shop (the fixture's cover URLs),
    # which is the same fetch the apply path later makes.
    expect(card.locator("img")).to_have_js_property("naturalWidth", 60)

    card.get_by_role("button", name="Übernehmen").click()

    # The apply response redirects to /media/<id>; the expect keeps waiting
    # across the navigation, so this also proves the redirect target renders.
    expect(page.locator("h1")).to_have_text("Unterwerfung")
    # The apply stored the cover: the detail page only appends the cache-
    # busting version when the row carries a sha256.
    expect(page.locator(".cover")).to_have_attribute("src", re.compile(r"/media/5/cover/md\.webp\?v="))
