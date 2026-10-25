"""REQ-PROJ-08: real browser on an explicitly supplied disposable project stack.

No provider or browser transport is mocked. Start/provision the stack separately.
Account path and URL are mandatory; screenshots and receipts go to BRAIN only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "BRAIN/project-mode-L3"


def api(page: Page, path: str) -> Any:
    return page.evaluate(
        """async path => {
      const r = await fetch(path, {credentials:'same-origin'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    }""",
        path,
    )


def dialog(page: Page, title: str, name: str) -> None:
    page.get_by_role("button", name=title, exact=True).click()
    modal = page.get_by_role("dialog")
    modal.get_by_role("textbox", name="Name", exact=True).fill(name)
    modal.get_by_role("button", name="Create", exact=True).click()
    expect(modal).to_have_count(0)


def files() -> list[Path]:
    from reportlab.pdfgen.canvas import Canvas
    from openpyxl import Workbook

    pdf = OUTPUT / "Project brief.pdf"
    canvas = Canvas(str(pdf))
    canvas.drawString(72, 760, "Orion delivery brief")
    canvas.drawString(
        72, 735, "The pilot serves 42 northern sites and 17 southern sites."
    )
    canvas.drawString(72, 710, "Delivery is due on 15 December 2026.")
    canvas.save()
    sheet = OUTPUT / "Budget.xlsx"
    workbook = Workbook()
    tab = workbook.active
    assert tab is not None
    tab.title = "Budget"
    tab.append(["Region", "Sites", "EUR"])
    tab.append(["North", 42, 42000])
    tab.append(["South", 17, 17000])
    workbook.save(sheet)
    return [pdf, sheet]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--navigation-only", action="store_true")
    args = parser.parse_args()
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
                data={k: account[k] for k in ("email", "password")},
            )
            assert response.status == 200, response.status
            token = response.json()["token"]
            context.add_init_script('localStorage.setItem("locale", "en");')
            page = context.new_page()
            page.goto(base, wait_until="domcontentloaded")
            page.evaluate('token => localStorage.setItem("token",token)', token)
            page.reload(wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(timeout=60000)
            page.keyboard.press("Escape")
            page.locator("#atlas-project-entry").click()
            dialog(page, "New project", "Orion browser " + str(int(time.time())))
            dialog(page, "New session", "Document comparison")
            expect(page.locator(".atlas-project-center")).to_contain_text(
                "Orion" if args.navigation_only else "Chat"
            )
            assert (
                "org=" in page.url
                and "project=" in page.url
                and "conversation=" in page.url
            )
            page.screenshot(path=str(OUTPUT / "navigation.png"))
            page.reload(wait_until="domcontentloaded")
            expect(
                page.locator(".atlas-project-left [aria-current=true]")
            ).to_have_text("Document comparison", timeout=30000)
            if args.navigation_only:
                print(
                    "project browser navigation, identity-scoped reload: PASS (local only)"
                )
                return
            page.locator(".atlas-project-composer input[type=file]").set_input_files(
                [str(p) for p in files()]
            )
            expect(page.locator(".atlas-project-notice")).to_have_text(
                "Saved", timeout=60000
            )
            input_box = page.get_by_role("textbox", name="Message", exact=True)
            input_box.fill(
                "Summarize both sources. Include the site counts, total budget, and delivery date. Do not modify originals."
            )
            page.locator("[data-send]").click()
            expect(page.locator("[data-send]")).to_be_enabled(timeout=180000)
            transcript = page.locator(".atlas-project-transcript")
            for term in ("42", "17", "59", "December"):
                expect(transcript).to_contain_text(term, timeout=10000)
            page.screenshot(path=str(OUTPUT / "chat-and-context.png"))
            receipt = {"url": page.url, "chat_useful": True}
            (OUTPUT / "receipt.json").write_text(json.dumps(receipt, indent=2))
            print("project browser Chat with PDF/spreadsheet: PASS (live)")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
