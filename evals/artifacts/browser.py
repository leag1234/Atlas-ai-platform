"""Playwright adapter for the artefact benchmark.

The runner only depends on the small protocol implemented here. Unit tests use
their own in-memory browser, so Playwright is imported lazily and never needed
offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


class Browser(Protocol):
    def open(self, url: str) -> None:
        """Open an artefact URL and wait for its ready state."""

    def screenshot(self, path: str, theme: str, phone: bool) -> None:
        """Capture a screenshot in the requested viewport and theme."""

    def interact(self, kind: str) -> bool:
        """Run a scripted interaction and return whether it changed the app."""

    def check_version(self, pattern: str) -> bool:
        """Return whether the new version exposes the requested control."""

    def export(self, path: str) -> bool:
        """Download, reopen from disk, and run the ready and console checks."""

    def close(self) -> None:
        """Release browser resources."""


@dataclass(frozen=True)
class ConsoleError(Exception):
    message: str


class PlaywrightBrowser:
    """Thin Playwright implementation of the benchmark browser protocol."""

    def __init__(self, base_url: str) -> None:
        from playwright.sync_api import ViewportSize, sync_playwright

        self.viewport_size = ViewportSize

        self.base_url = base_url.rstrip("/")
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir="/tmp/atlas-artifact-bench-profile",
            headless=True,
        )
        self.page = self.context.new_page()
        self.console_errors: list[str] = []
        self.page.on("console", self._on_console)
        self.export_errors: list[str] = []

    def _on_console(self, message: Any) -> None:
        if message.type == "error":
            self.console_errors.append(str(message.text))

    def open(self, url: str) -> None:
        self.page.goto(self.base_url + url, wait_until="domcontentloaded")
        self.page.wait_for_load_state("networkidle")
        if self.console_errors:
            raise ConsoleError(self.console_errors[0])

    def screenshot(self, path: str, theme: str, phone: bool) -> None:
        viewport = (
            self.viewport_size(width=390, height=844)
            if phone
            else self.viewport_size(width=1280, height=800)
        )
        self.page.set_viewport_size(viewport)
        color_scheme: Literal["dark", "light"] = "dark" if theme == "dark" else "light"
        self.page.emulate_media(color_scheme=color_scheme)
        self.page.screenshot(path=path, full_page=True)

    def interact(self, kind: str) -> bool:
        if kind == "keyboard":
            before: bytes = self.page.locator("canvas").screenshot()
            for key in ("ArrowUp", "ArrowRight", "ArrowDown", "ArrowLeft"):
                self.page.keyboard.press(key)
                self.page.wait_for_timeout(500)
            after: bytes = self.page.locator("canvas").screenshot()
            return before != after
        if kind == "timer":
            self.page.get_by_role("button", name="start", exact=False).click()
            text_before = self.page.inner_text("body")
            self.page.wait_for_timeout(2000)
            return self.page.inner_text("body") != text_before
        if kind == "form":
            inputs = self.page.locator("input[type=number]")
            for index, value in enumerate(("120", "10", "4")):
                inputs.nth(index).fill(value)
            return "33" in self.page.inner_text("body")
        if kind == "flashcards":
            card = self.page.locator("button, [role=button], .card").first
            text_before = card.inner_text()
            card.click()
            return card.inner_text() != text_before
        if kind == "chart":
            text_before = self.page.inner_text("body")
            self.page.get_by_role("button", name="ordin", exact=False).click()
            return self.page.inner_text("body") != text_before
        raise ValueError(f"unsupported interaction: {kind}")

    def has_touch_control(self) -> bool:
        return self.page.locator("button, [role=button], input, canvas").count() > 0

    def check_version(self, pattern: str) -> bool:
        text = self.page.inner_text("body")
        controls = self.page.locator(
            "button, input, select, textarea, label"
        ).all_inner_texts()
        return pattern.lower() in text.lower() or any(
            pattern.lower() in control.lower() for control in controls
        )

    def export(self, path: str) -> bool:
        with self.page.expect_download() as download_event:
            download = download_event.value
            download.save_as(path)
        disk_page = self.context.new_page()
        disk_errors: list[str] = []
        disk_page.on(
            "console",
            lambda message: disk_errors.append(str(message.text))
            if message.type == "error"
            else None,
        )
        disk_page.goto("file://" + path, wait_until="domcontentloaded")
        disk_page.wait_for_load_state("networkidle")
        passed = not disk_errors
        disk_page.close()
        return passed

    def close(self) -> None:
        self.context.close()
        self.playwright.stop()
