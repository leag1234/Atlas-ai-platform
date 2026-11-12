"""Offline browser contract for the app side view.

With ATLAS_RENDER_CHAT_URL set, the page is the local Open WebUI stack. Without
it, a bare shell that loads services/orchestrator/chat-ui.js is served to the
browser by this script: nothing else has to run.
"""

import os
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, Route
from playwright.sync_api import expect, sync_playwright

SHELL_URL = "http://atlas.test/"
URL = os.environ.get("ATLAS_RENDER_CHAT_URL", SHELL_URL)
ARTIFACT = "0123456789abcdef0123456789abcdef"
PREFIX = f"/api/atlas/artifacts/{ARTIFACT}/versions"
SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body>
<div style="display: contents"><div id="messages-container"></div></div>
<script src="/static/atlas/chat-ui.js"></script></body></html>"""
APP = """<!doctype html><html><head><title>Probe</title></head>
<body><h1>Version {version}</h1><button id="navigate" onclick="location.href='/other.html'">Navigate</button></body></html>"""


def install(
    page: Page, version: int, label: str = "Open probe", suffix: str = ""
) -> None:
    """Render an answer that links one version, as the chat does after a turn."""
    page.evaluate(
        """({version, label, href}) => {
          const host = document.querySelector('#messages-container') || document.body;
          const message = document.createElement('div');
          message.id = `message-app-${version}`;
          message.className = 'chat-assistant';
          message.dataset.appFixture = String(version);
          const prose = document.createElement('div'), link = document.createElement('a');
          prose.className = 'markdown-prose'; link.href = href; link.textContent = label;
          prose.append(link); message.append(prose); host.append(message);
        }""",
        {
            "version": version,
            "label": label,
            "href": f"{PREFIX}/{version}/open{suffix}#atlas-view=app"
            f"&artifact={ARTIFACT}&version={version}&title=Probe",
        },
    )


def remove(page: Page, version: int | None = None) -> None:
    selector = (
        "[data-app-fixture]" if version is None else f'[data-app-fixture="{version}"]'
    )
    page.evaluate(
        "selector => document.querySelectorAll(selector).forEach(node => node.remove())",
        selector,
    )


def offline(route: Route) -> None:
    path = route.request.url.split("://", 1)[1].partition("/")[2].split("#")[0]
    if route.request.url == SHELL_URL:
        route.fulfill(content_type="text/html", body=SHELL)
    elif path == "static/atlas/chat-ui.js":
        route.fulfill(
            content_type="text/javascript",
            body=Path("services/orchestrator/chat-ui.js").read_text(),
        )
    else:
        route.fulfill(status=404, body="")


def app(version: int, headers: dict[str, str]) -> Callable[[Route], None]:
    def fulfill(route: Route) -> None:
        route.fulfill(
            content_type="text/html", body=APP.format(version=version), headers=headers
        )

    return fulfill


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_context(
            viewport={"width": 1500, "height": 1000}, locale="en-US"
        ).new_page()
        if URL == SHELL_URL:
            page.route("**/*", offline)
        for version in (1, 2):
            page.route(f"**{PREFIX}/{version}/open", app(version, {}))
            page.route(
                f"**{PREFIX}/{version}/download",
                app(version, {"Content-Disposition": "attachment"}),
            )
        page.goto(URL, wait_until="networkidle")
        page.wait_for_function("window.atlasSideView")
        install(page, 1)
        frame = page.locator(".atlas-side-frame")
        frame.wait_for()
        expect(frame).to_have_attribute("sandbox", "allow-scripts")
        assert frame.get_attribute("allow") == ""
        assert frame.get_attribute("src") == f"{PREFIX}/1/open"
        heading = page.frame_locator(".atlas-side-frame").locator("h1")
        expect(heading).to_have_text("Version 1")
        assert page.locator(".atlas-side-stopped").count() == 0
        expect(page.locator(".atlas-side-version")).to_have_text("Version 1")
        expect(page.locator(".atlas-side-notice")).to_contain_text(
            "Temporary preview: data entered in the app is not kept."
        )
        expect(page.get_by_role("button", name="Open in your browser")).to_be_hidden()
        with page.expect_download() as download:
            page.get_by_role("button", name="Download", exact=True).click()
        assert download.value.url.endswith(f"{PREFIX}/1/download")
        # A link that is not an answer footer gets the interface wording.
        chips = page.locator(".chat-assistant a.atlas-side-chip")
        expect(chips.first).to_have_text("Open in side view")

        # The conversation produces version 2 while version 1 is open and was
        # used: the panel shows version 2 by itself, without a bar or a click.
        page.evaluate("document.querySelector('.atlas-side-frame').focus()")
        install(page, 2, "Ouvrir « Probe » (version 2)")
        expect(page.locator(".atlas-side-version")).to_have_text("Version 2")
        expect(heading).to_have_text("Version 2")
        assert page.locator(".atlas-side-banner").count() == 0
        # The chip of a French answer footer stays French in an English interface.
        expect(chips.last).to_have_text("Ouvrir dans la vue latérale")

        # Back on version 1 by choice: version 2, already met and rendered again,
        # is offered and never imposed.
        chips.first.click()
        expect(page.locator(".atlas-side-version")).to_have_text("Version 1")
        remove(page, 2)
        install(page, 2)
        expect(page.locator(".atlas-side-banner")).to_have_text(
            "New version availableReload"
        )
        expect(page.locator(".atlas-side-version")).to_have_text("Version 1")
        page.get_by_role("button", name="Reload", exact=True).click()
        expect(page.locator(".atlas-side-version")).to_have_text("Version 2")
        assert page.locator(".atlas-side-banner").count() == 0

        page.frame_locator(".atlas-side-frame").locator("#navigate").click()
        expect(page.locator(".atlas-side-stopped")).to_contain_text(
            "The app tried to open another page and was stopped."
        )
        page.get_by_role("button", name="Restart app", exact=True).click()
        expect(heading).to_have_text("Version 2")

        # A link whose path and fragment disagree is never opened.
        page.evaluate("window.atlasSideView.close()")
        remove(page)
        install(page, 1, suffix="-invalid")
        page.wait_for_timeout(300)
        assert page.locator(".atlas-side-frame").count() == 0
        browser.close()


if __name__ == "__main__":
    main()
