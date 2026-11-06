"""REQ-PROJ-08: move native chats from Open WebUI menus in a real browser."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Page, expect, sync_playwright

from project_mode_browser import api, dialog


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "BRAIN/project-move-menu"


def native_chat(page: Page, title: str) -> str:
    chat = api(page, "/api/v1/chats/")
    item = next(value for value in chat["items"] if value["title"] == title)
    return str(item["id"])


def open_row_menu(page: Page, chat_id: str) -> Locator:
    row = page.locator(f'a[href="/c/{chat_id}"]')
    row.get_by_role("button", name="Chat Menu", exact=True).click()
    item = page.locator(
        f'#sidebar-chat-group:has(a[href="/c/{chat_id}"]) '
        ".app-dropdown-menu [data-atlas-move-project]"
    ).last
    expect(item).to_be_visible()
    box = item.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] > 0
    assert box["x"] >= 0 and box["y"] >= 0
    viewport = page.viewport_size
    assert viewport is not None
    assert box["x"] + box["width"] <= viewport["width"]
    assert box["y"] + box["height"] <= viewport["height"]
    return item


def create_native_chat(page: Page, title: str) -> str:
    page.goto(base_url(page) + "/", wait_until="domcontentloaded")
    page.locator("#chat-input").wait_for(timeout=60000)
    page.get_by_role("textbox", name="Message", exact=True).fill("Move source")
    page.locator("[data-send]").click()
    expect(page.locator("[data-send]")).to_be_enabled(timeout=60000)
    return native_chat(page, title)


def base_url(page: Page) -> str:
    return os.environ["ATLAS_PROJECT_BROWSER_URL"].rstrip("/")


def open_header_menu(page: Page) -> Locator:
    page.locator("#chat-context-menu-button").click()
    item = page.locator(".app-dropdown-menu [data-atlas-move-project]").last
    expect(item).to_be_visible()
    return item


def assert_menu_has_no_move_item(page: Page, trigger: Locator) -> None:
    trigger.click()
    expect(page.locator(".app-dropdown-menu").last).to_be_visible()
    expect(page.locator(".app-dropdown-menu [data-atlas-move-project]")).to_have_count(
        0
    )
    page.keyboard.press("Escape")


def move_from_menu(page: Page, item: Locator, project_name: str) -> None:
    item.click()
    submenu = page.locator(".atlas-project-move-menu")
    expect(submenu).to_contain_text(project_name)
    submenu.get_by_role("button", name=project_name, exact=True).click()
    expect(page.locator(".atlas-project-center")).to_contain_text(project_name)
    assert "atlas_projects=1" in page.url and "conversation=" in page.url


def create_from_menu(page: Page, item: Locator, project_name: str) -> None:
    item.click()
    submenu = page.locator(".atlas-project-move-menu")
    submenu.get_by_role("button", name="+ New project", exact=True).click()
    modal = page.get_by_role("dialog")
    modal.get_by_role("textbox", name="Name", exact=True).fill(project_name)
    modal.get_by_role("button", name="Create", exact=True).click()
    expect(page.locator(".atlas-project-center")).to_contain_text(project_name)
    assert "atlas_projects=1" in page.url and "conversation=" in page.url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    base = os.environ["ATLAS_PROJECT_BROWSER_URL"].rstrip("/")
    target = urlsplit(base)
    assert target.hostname in {"127.0.0.1", "localhost"}
    assert target.port not in {None, 8000, 8020, 3000, 5432}
    account = json.loads(Path(os.environ["ATLAS_PROJECT_BROWSER_ACCOUNT"]).read_text())
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 960}, locale="en-US"
        )
        try:
            response = context.request.post(
                base + "/api/v1/auths/signin",
                data={key: account[key] for key in ("email", "password")},
            )
            assert response.status == 200, response.status
            token = response.json()["token"]
            context.add_init_script('localStorage.setItem("locale", "en");')
            page = context.new_page()
            page.goto(base, wait_until="domcontentloaded")
            page.evaluate("token => localStorage.setItem('token',token)", token)
            page.reload(wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(timeout=60000)
            page.keyboard.press("Escape")
            page.locator("#atlas-project-entry").click()
            page.keyboard.press("Escape")
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(timeout=60000)

            dialog(page, "New project", "Move target")
            page.keyboard.press("Escape")
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(timeout=60000)

            first_id = create_native_chat(page, "Move source one")
            second_id = create_native_chat(page, "Move source two")
            page.goto(base + f"/c/{first_id}", wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(timeout=60000)
            assert_menu_has_no_move_item(page, page.locator("#user-avatar"))
            assert_menu_has_no_move_item(page, page.locator("#input-menu-button"))
            for theme in ("light", "dark"):
                page.emulate_media(color_scheme=theme)
                item = open_row_menu(page, first_id)
                page.screenshot(path=str(OUTPUT / f"row-{theme}.png"))
                page.keyboard.press("Escape")
            item = open_row_menu(page, second_id)
            move_from_menu(page, item, "Move target")
            assert api(page, f"/api/v1/chats/{second_id}")["detail"] == "Not found"
            assert api(page, f"/api/v1/chats/{first_id}")["id"] == first_id

            page.goto(base + f"/c/{first_id}", wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(timeout=60000)
            item = open_header_menu(page)
            created = "Created by menu " + str(int(time.time()))
            create_from_menu(page, item, created)
            assert api(page, f"/api/v1/chats/{first_id}")["detail"] == "Not found"

            third_id = create_native_chat(page, "Move source three")
            page.set_viewport_size({"width": 390, "height": 844})
            item = open_row_menu(page, third_id)
            item.click()
            modal = page.get_by_role("dialog")
            expect(modal).to_contain_text("Move target")
            modal.get_by_role("button", name="Move target", exact=True).click()
            expect(page.locator(".atlas-project-center")).to_contain_text("Move target")
            assert api(page, f"/api/v1/chats/{third_id}")["detail"] == "Not found"
            print("project move menu browser: PASS (local only)")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
