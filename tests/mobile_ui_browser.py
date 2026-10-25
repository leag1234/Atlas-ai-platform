"""Playwright regression for the trial display control and recorded appearance reply."""

import os
from pathlib import Path
import signal

from playwright.sync_api import Page, expect, sync_playwright


URL = os.environ.get("ATLAS_MOBILE_UI_URL", "http://127.0.0.1:3312")
OUTPUT = Path("BRAIN/mobile-ui-fixes")
EMAIL = "mobile-ui@example.test"
PASSWORD = os.environ.get("ATLAS_MOBILE_UI_PASSWORD", "local-disposable-password")
RUN_TIMEOUT_SECONDS = 240


def take_failure_screenshot(page: Page) -> None:
    try:
        page.screenshot(
            path=str(OUTPUT / "failure.png"), full_page=True, timeout=10_000
        )
    except Exception:
        pass


def authenticate(page: Page) -> None:
    """Create the disposable first admin without relying on Svelte form bindings."""
    response = page.context.request.post(
        URL + "/api/v1/auths/signup",
        data={"name": "Mobile UI Test", "email": EMAIL, "password": PASSWORD},
    )
    assert response.status == 200, response.text()
    token = response.json()["token"]
    page.context.add_cookies([{"name": "token", "value": token, "url": URL}])
    page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    page.evaluate(
        "token => {localStorage.setItem('token', token); localStorage.setItem('locale', 'en');}",
        token,
    )
    page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    page.locator("#chat-input").wait_for(timeout=45_000)


def boxes_overlap(first: dict[str, float], second: dict[str, float]) -> bool:
    return (
        first["left"] < second["right"]
        and second["left"] < first["right"]
        and first["top"] < second["bottom"]
        and second["top"] < first["bottom"]
    )


def host_header_boxes(page: Page) -> list[dict[str, float]]:
    boxes: list[dict[str, float]] = (
        page.evaluate("""() => [...document.querySelectorAll('button,a,[role="button"]')]
      .filter(node => !node.closest('#atlas-ui-selector'))
      .map(node => node.getBoundingClientRect())
      .filter(box => box.width && box.height && box.top < 84 && box.right > innerWidth / 2)
      .map(box => ({left: box.left, right: box.right, top: box.top, bottom: box.bottom}))""")
    )
    return boxes


def assert_clear_of_host_controls(page: Page, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    toggle = page.locator("#atlas-ui-toggle")
    expect(toggle).to_be_visible(timeout=20_000)
    toggle_rect = toggle.bounding_box()
    assert toggle_rect is not None
    toggle_box: dict[str, float] = {
        "left": toggle_rect["x"],
        "right": toggle_rect["x"] + toggle_rect["width"],
        "top": toggle_rect["y"],
        "bottom": toggle_rect["y"] + toggle_rect["height"],
    }
    controls = host_header_boxes(page)
    assert controls, "Open WebUI header controls were not rendered"
    assert not any(boxes_overlap(toggle_box, control) for control in controls), {
        "toggle": toggle_box,
        "host_controls": controls,
    }
    page.screenshot(path=str(OUTPUT / f"control-{width}x{height}.png"), full_page=True)


def replay_commons_delivery(page: Page) -> None:
    """Render the recorded delivery shape without another Commons network request."""
    page.evaluate(
        """text => {
          const reply = document.createElement('article');
          reply.className = 'chat-assistant'; reply.dataset.atlasAppearanceReplay = 'commons';
          const prose = document.createElement('div'); prose.className = 'markdown-prose';
          const paragraph = document.createElement('p'); paragraph.textContent = text;
          const image = document.createElement('img'); image.alt = 'Recorded Commons image';
          image.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="320" height="160"%3E%3Crect width="100%25" height="100%25" fill="%236b8e23"/%3E%3C/svg%3E';
          prose.append(paragraph, image); reply.append(prose); document.body.append(reply);
        }""",
        "A useful explanation.",
    )
    replay = page.locator("[data-atlas-appearance-replay='commons']")
    expect(replay.get_by_role("img", name="Recorded Commons image")).to_be_visible()
    expect(replay).not_to_contain_text("<?xml version")
    expect(replay).not_to_contain_text("<!DOCTYPE")
    page.screenshot(path=str(OUTPUT / "commons-replay.png"), full_page=True)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    signal.alarm(RUN_TIMEOUT_SECONDS)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(30_000)
        try:
            authenticate(page)
            before_style = page.add_style_tag(
                content="#atlas-ui-selector { right:20px; }"
            )
            page.screenshot(path=str(OUTPUT / "mobile-before.png"), full_page=True)
            before_style.evaluate("node => node.remove()")
            for viewport in ((390, 844), (390, 520), (1280, 800)):
                assert_clear_of_host_controls(page, *viewport)
            toggle = page.locator("#atlas-ui-toggle")
            toggle.click()
            expect(page.locator("#atlas-ui-popover")).to_be_visible()
            assert page.get_by_label("Read Aloud", exact=True).count() == 0
            assert "orchestrator:" not in page.locator("body").inner_text()
            replay_commons_delivery(page)
        except Exception:
            take_failure_screenshot(page)
            raise
        finally:
            page.close()
            browser.close()
            signal.alarm(0)
    print("mobile UI browser: header clear, audio hidden, Commons replay clean")


if __name__ == "__main__":
    main()
