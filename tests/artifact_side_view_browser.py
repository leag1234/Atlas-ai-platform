"""Offline browser contract for the app side view on the Open WebUI shell."""

import os
from playwright.sync_api import Page
from playwright.sync_api import expect, sync_playwright

URL = os.environ["ATLAS_RENDER_CHAT_URL"]
ARTIFACT = "0123456789abcdef0123456789abcdef"
OPEN = f"/api/atlas/artifacts/{ARTIFACT}/versions/1/open"
DOWNLOAD = f"/api/atlas/artifacts/{ARTIFACT}/versions/1/download"
APP = """<!doctype html><html><head><title>Probe</title></head>
<body><h1>Version one</h1><button id="navigate" onclick="location.href='/other.html'">Navigate</button></body></html>"""


def install(page: Page, version: int, path: str = OPEN) -> None:
    page.evaluate(
        """({version, path}) => {
          document.querySelectorAll('[data-app-fixture]').forEach(node => node.remove());
          const host = document.querySelector('#messages-container') || document.body;
          const message = document.createElement('div');
          message.id = `message-app-${version}`;
          message.className = 'chat-assistant';
          message.dataset.appFixture = 'yes';
          message.innerHTML = `<div class="markdown-prose"><a href="${path}#atlas-view=app&artifact=${'0123456789abcdef0123456789abcdef'}&version=${version}&title=Probe">Open probe</a></div>`;
          host.append(message);
        }""",
        {"version": version, "path": path},
    )


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.route(
            f"**{OPEN}",
            lambda route: route.fulfill(content_type="text/html", body=APP),
        )
        page.route(
            f"**{DOWNLOAD}",
            lambda route: route.fulfill(
                content_type="text/html",
                body=APP,
                headers={"Content-Disposition": "attachment"},
            ),
        )
        page.goto(URL, wait_until="networkidle")
        install(page, 1)
        frame = page.locator(".atlas-side-frame")
        frame.wait_for()
        expect(frame).to_have_attribute("sandbox", "allow-scripts")
        assert frame.get_attribute("allow") == ""
        assert frame.get_attribute("src") == OPEN
        expect(page.frame_locator(".atlas-side-frame").locator("h1")).to_have_text(
            "Version one"
        )
        assert page.locator(".atlas-side-stopped").count() == 0
        assert page.evaluate("window.atlasSideView.loads") == 1
        expect(page.locator(".atlas-side-version")).to_have_text("Version 1")
        expect(page.locator(".atlas-side-notice")).to_contain_text(
            "Temporary preview: data entered in the app is not kept."
        )
        expect(page.get_by_role("button", name="Open in your browser")).to_be_hidden()
        with page.expect_download() as download:
            page.get_by_role("button", name="Download", exact=True).click()
        assert download.value.url.endswith(DOWNLOAD)
        page.evaluate("document.querySelector('.atlas-side-frame').focus()")
        install(page, 2)
        expect(page.locator(".atlas-side-banner")).to_have_text(
            "New version availableReload"
        )
        page.get_by_role("button", name="Reload", exact=True).click()
        expect(page.locator(".atlas-side-version")).to_have_text("Version 2")
        assert page.locator(".atlas-side-banner").count() == 0
        page.frame_locator(".atlas-side-frame").locator("#navigate").click()
        expect(page.locator(".atlas-side-stopped")).to_contain_text(
            "The app tried to open another page and was stopped."
        )
        page.get_by_role("button", name="Restart app", exact=True).click()
        expect(page.frame_locator(".atlas-side-frame").locator("h1")).to_have_text(
            "Version one"
        )
        page.evaluate("window.atlasSideView.close()")
        install(page, 1, OPEN + "-invalid")
        assert page.locator(".atlas-side-frame").count() == 0
        browser.close()


if __name__ == "__main__":
    main()
