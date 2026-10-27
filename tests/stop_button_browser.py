"""Playwright acceptance for native Open WebUI Stop on desktop and mobile.

Run against an already-started disposable quick-start stack. The long answer is
cancelled promptly; the brief follow-up proves the same conversation remains usable.
"""

import os
import time
from pathlib import Path

from playwright.sync_api import Browser, Page, ViewportSize, expect, sync_playwright


URL = os.environ.get("ATLAS_STOP_BROWSER_URL", "http://127.0.0.1:3311")
OUTPUT = Path("BRAIN/stop-button-browser")


def sign_in(page: Page) -> None:
    # The disposable trial starts with no users.  The test intentionally uses a
    # fresh local-only account and never writes its credentials to disk.
    page.goto(URL, wait_until="networkidle")
    if page.get_by_text("Get started", exact=True).count():
        page.get_by_text("Get started", exact=True).last.click()
        page.locator("input").nth(0).fill("Stop Button Test")
        page.locator("input").nth(1).fill("stop-button@example.test")
        page.locator("input").nth(2).fill("local-disposable-password")
        page.get_by_text("Create Admin Account", exact=True).click(no_wait_after=True)
        page.locator("#chat-input").wait_for(timeout=30_000)
    elif page.locator("#chat-input").count() == 0:
        page.locator('input[type="email"]').fill("stop-button@example.test")
        page.locator('input[type="password"]').fill("local-disposable-password")
        page.get_by_text("Sign in", exact=True).click(no_wait_after=True)
        page.locator("#chat-input").wait_for(timeout=30_000)


def run_viewport(browser: Browser, name: str, viewport: ViewportSize) -> None:
    page = browser.new_page(viewport=viewport)
    try:
        sign_in(page)
        # v0.11.3 may show its release-notes dialog after first sign-in.
        page.keyboard.press("Escape")
        page.locator("#chat-input").fill("Explain a difficult subject at great length.")
        expect(page.locator("#send-message-button")).to_be_visible()
        page.locator("#send-message-button").click(no_wait_after=True, timeout=5_000)
        stop = page.locator("#stop-response-button")
        expect(stop).to_be_visible(timeout=15_000)
        expect(stop).to_be_enabled()
        expect(stop).to_have_attribute("aria-label", "Stop generating")
        stop.focus()
        expect(stop).to_be_focused()
        started = time.monotonic()
        stop.click(no_wait_after=True)
        expect(page.locator(".atlas-stop-mark")).to_have_text("Stopped.", timeout=2_000)
        assert time.monotonic() - started < 2
        page.locator("#chat-input").fill("What is 2 + 2?")
        expect(page.locator("#send-message-button")).to_be_visible(timeout=5_000)
        page.locator("#send-message-button").click(no_wait_after=True)
        expect(page.locator(".chat-assistant").last).to_contain_text(
            "4", timeout=30_000
        )
        page.screenshot(path=str(OUTPUT / f"{name}.png"), full_page=True)
    finally:
        page.close()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            run_viewport(browser, "desktop", {"width": 1280, "height": 900})
            run_viewport(browser, "mobile", {"width": 390, "height": 844})
        finally:
            browser.close()
    print("stop-button browser: desktop=pass mobile=pass")


if __name__ == "__main__":
    main()
