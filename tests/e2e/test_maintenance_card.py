"""The settings page's maintenance card is HTMX-driven; the browser proves
the round trip: press a button, the card swaps itself, the header reports the
finished task, and a failed request surfaces as a toast."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from bib_tracker import __version__

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_pressing_rebuild_reports_the_finished_task(app_server: str, page: Page) -> None:
    page.goto(f"{app_server}/settings")
    card = page.locator("#maintenance-card")
    expect(card).to_be_visible()
    expect(card.get_by_text("Wartung")).to_be_visible()

    card.get_by_role("button", name="Ausführen").nth(4).click()  # Verlauf neu aufbauen
    # The POST returns the whole card; the header then carries the summary.
    expect(card.get_by_text("zuletzt:")).to_be_visible(timeout=10_000)
    expect(card.get_by_text("Verlauf neu aufbauen", exact=True)).to_be_visible()
    expect(card.get_by_text("abgebrochen")).to_have_count(0)


def test_the_disabled_enrichment_marks_the_provider_tasks(app_server: str, page: Page) -> None:
    """This server runs with enrichment off: the card must say so and the
    provider-hitting buttons must be inert, while the local ones stay live."""
    page.goto(f"{app_server}/settings")
    card = page.locator("#maintenance-card")
    expect(card.locator('.notice[data-tone="warn"]')).to_be_visible()
    expect(card.get_by_role("button", name="Ausführen").first).to_be_disabled()
    expect(card.get_by_role("button", name="Ausführen").nth(4)).to_be_enabled()


def test_the_footer_names_the_running_version(app_server: str, page: Page) -> None:
    page.goto(f"{app_server}/")
    expect(page.locator(".app-footer")).to_contain_text(__version__)
    expect(page.locator(".app-footer")).to_contain_text("Schema")


def test_a_failed_request_surfaces_as_a_toast(app_server: str, page: Page) -> None:
    """A 409/404 from the maintenance endpoints used to swap nothing, so the
    press just sat there; the response-error listener makes it say why."""
    page.goto(f"{app_server}/settings")
    page.evaluate("htmx.ajax('POST', '/api/media/999/crawl', {target: 'body', swap: 'outerHTML'})")
    toast = page.locator('.toast[data-tone="bad"]')
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("existiert nicht")
