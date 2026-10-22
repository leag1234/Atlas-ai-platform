"""Real-page Code UI acceptance: real Docker tools, scripted model or capped live task."""

import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from playwright.sync_api import Page, expect, sync_playwright

from test_code_mode import archive

OUTPUT = Path("BRAIN/code-mode-ui")
LIVE = "--live" in sys.argv or "--review-live" in sys.argv
URL = "http://127.0.0.1:" + ("9300" if LIVE else "9301")
FILES = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "test_calc.py": "import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
}


def snapshot(page: Page, name: str) -> None:
    for width in (1280, 1500):
        page.set_viewport_size({"width": width, "height": 1000})
        page.screenshot(path=str(OUTPUT / f"{name}-{width}.png"), full_page=True)
        assert page.locator("#atlas-code-panel").evaluate(
            "e => e.getBoundingClientRect().right <= innerWidth"
        ), name


def load(page: Page, fresh: bool = False) -> None:
    if fresh:
        page.evaluate("sessionStorage.removeItem('atlas-code:' + location.pathname)")
    page.goto(URL, wait_until="networkidle")
    welcome = page.get_by_text("Okay, Let's Go!", exact=True)
    if welcome.count():
        welcome.click()
    page.locator("#atlas-code-entry").wait_for(timeout=30000)
    page.locator("#atlas-code-entry").click()


def upload(page: Page) -> None:
    page.get_by_label("Import project archive").set_input_files(
        {
            "name": "tiny-project.zip",
            "mimeType": "application/zip",
            "buffer": archive(FILES),
        }
    )
    expect(page.locator(".atlas-code-status")).to_contain_text("ready to import")


def send(page: Page, text: str) -> None:
    page.get_by_label("Code message", exact=True).fill(text)
    page.get_by_label("Code message", exact=True).press("Enter")


def ready(page: Page) -> None:
    expect(page.locator(".atlas-code-status")).to_have_text("Ready", timeout=180000)
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_disabled()


def mode(page: Page, **values: Any) -> None:
    result = page.request.post("http://127.0.0.1:14321/testing/mode", data=values)
    assert result.ok


def main() -> None:
    assert os.environ.get("ATLAS_PORT_BASE") == "6300"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1500, "height": 1000},
            permissions=["clipboard-read", "clipboard-write"],
        )
        if "--resume" in sys.argv:
            events = json.loads((OUTPUT / "live-audit.json").read_text())
            recap = next(
                event["output"]["text"] for event in events if event["type"] == "recap"
            )
            saved = {
                "id": events[0]["session_id"],
                "imported": True,
                "queue": [],
                "messages": [
                    {
                        "user": "Fix calc.py: add(a, b) must return a + b, not a - b. Run python3 -m unittest -v. Use at most two tool steps and finish with a two-sentence summary. Do not search or use the network.",
                        "answer": recap,
                        "events": events,
                    }
                ],
            }
            context.add_init_script(
                "sessionStorage.setItem('atlas-code:/', "
                + json.dumps(json.dumps(saved))
                + ")"
            )
        if "--review-live" in sys.argv:
            context.add_init_script(
                "sessionStorage.setItem('atlas-code:/', "
                + json.dumps((OUTPUT / "live-evidence.json").read_text())
                + ")"
            )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        load(page)
        if "--review-live" in sys.argv:
            expect(
                page.get_by_text("Full checked response", exact=True)
            ).to_be_visible()
            page.locator(".atlas-code-step summary", has_text="run_command").click()
            page.locator(".atlas-code-transcript").evaluate(
                "e => e.scrollTop = e.scrollHeight"
            )
            snapshot(page, "live-restored")
            page.get_by_role("button", name="Review diff", exact=False).last.click()
            expect(page.locator(".atlas-side-diff")).to_contain_text("a + b")
            snapshot(page, "live-restored-diff")
            browser.close()
            print("Inspected stored real live events; no model request sent")
            return
        if "--resume" not in sys.argv:
            upload(page)
        if LIVE:
            send(
                page,
                "The previous two-tool-step restriction is lifted. The files are already read. Use run_command now to execute exactly this shell command: `sed -i 's/return a - b/return a + b/' calc.py && python3 -m unittest -v`. Then finish with two sentences based on the real output. No more reading is needed; do not search or use the network."
                if "--resume" in sys.argv
                else "Fix calc.py: add(a, b) must return a + b, not a - b. Run python3 -m unittest -v. Use at most two tool steps and finish with a two-sentence summary. Do not search or use the network.",
            )
            ready(page)
            snapshot(page, "live")
            data = page.evaluate(
                "JSON.parse(sessionStorage.getItem('atlas-code:' + location.pathname))"
            )
            (OUTPUT / "live-evidence.json").write_text(json.dumps(data, indent=2))
            expect(
                page.locator(".atlas-code-step summary", has_text="run_command")
            ).to_have_count(1)
            page.locator(".atlas-code-step summary", has_text="run_command").click()
            expect(
                page.locator(".atlas-code-output").filter(has_text="Exit code: 0")
            ).to_have_count(1)
            expect(page.get_by_role("link", name="Download patch").last).to_be_visible()
            snapshot(page, "live")
            page.get_by_role("button", name="Review diff", exact=False).last.click()
            expect(page.locator(".atlas-side-diff")).to_contain_text("a + b")
            snapshot(page, "live-diff")
            data = page.evaluate(
                "JSON.parse(sessionStorage.getItem('atlas-code:' + location.pathname))"
            )
            (OUTPUT / "live-evidence.json").write_text(json.dumps(data, indent=2))
        else:
            snapshot(page, "entry")
            send(page, "Fix this project")
            approval = page.get_by_role("region", name="Approval required")
            expect(
                approval.get_by_role("button", name="Allow", exact=True)
            ).to_be_visible(timeout=90000)
            expect(page.locator(".atlas-code-plan")).to_contain_text("Repair addition")
            expect(
                page.locator('.atlas-code-plan [data-status="in_progress"]')
            ).to_have_count(1)
            page.locator(
                ".atlas-code-step summary", has_text="run_command"
            ).first.click()
            expect(
                page.locator(".atlas-code-output").filter(has_text="Exit code: 1")
            ).to_be_visible()
            snapshot(page, "approval")
            page.get_by_role("button", name="Review diff", exact=False).click()
            expect(page.locator(".atlas-side-diff")).to_contain_text(
                "-    return a - b"
            )
            expect(page.locator(".atlas-side-diff")).to_contain_text(
                "+    return a + b"
            )
            snapshot(page, "diff")
            page.get_by_role("button", name="Close side view", exact=True).click()
            approval.get_by_role("button", name="Allow", exact=True).click()
            ready(page)
            expect(approval).to_contain_text("Approval allowed")
            expect(
                page.locator('.atlas-code-final-plan [data-status="completed"]')
            ).to_have_count(2)
            expect(page.locator(".atlas-code-question")).to_have_text(
                "What would you like to do next?"
            )
            expect(page.get_by_role("link", name="Download workspace")).to_be_visible()
            for name in ("Download workspace", "Download patch"):
                href = page.get_by_role("link", name=name).get_attribute("href")
                assert href
                response = page.request.get(href)
                assert response.ok, (name, response.status)
                assert len(response.body()) > 10
            page.locator(".atlas-code-answer .atlas-code-command").get_by_role(
                "button", name="Copy", exact=True
            ).click()
            assert (
                page.evaluate("navigator.clipboard.readText()")
                == "python3 -m unittest -v\n"
            )
            snapshot(page, "recap")
            identity = page.locator(".atlas-code-session").inner_text()
            page.get_by_role("button", name="Review the changes", exact=True).click()
            expect(page.locator(".atlas-code-user")).to_have_count(2)
            ready(page)
            assert page.locator(".atlas-code-session").inner_text() == identity
            # Reload rehydrates the same session and historical activity.
            load(page)
            assert page.locator(".atlas-code-session").inner_text() == identity
            expect(page.locator(".atlas-code-user")).to_have_count(2)
            send(page, "Stop this command")
            expect(
                page.locator('.atlas-code-step[data-status="running"] summary')
            ).to_contain_text("run_command", timeout=60000)
            send(page, "Queue followup")
            expect(page.locator(".atlas-code-queue")).to_contain_text("Queue followup")
            snapshot(page, "queue")
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.locator(".atlas-code-user")).to_have_count(4, timeout=60000)
            ready(page)
            expect(
                page.locator(".atlas-code-notice").filter(has_text="Stopped by you")
            ).to_be_visible()
            snapshot(page, "stopped")
            evidence = page.request.get(
                "http://127.0.0.1:14321/testing/evidence"
            ).json()
            assert evidence["active"] == [] and evidence["approvals"] == 0, evidence
            load(page, fresh=True)
            upload(page)
            send(page, "Repair the failing check")
            expect(
                approval.get_by_role("button", name="Deny", exact=True)
            ).to_be_visible(timeout=90000)
            approval.get_by_role("button", name="Deny", exact=True).click()
            ready(page)
            expect(approval).to_contain_text("Approval denied")
            send(page, "Honesty check")
            ready(page)
            expect(
                page.locator(".atlas-code-notice").filter(
                    has_text="Test claim corrected"
                )
            ).to_be_visible()
            mode(page, calls="1")
            send(page, "Limit this turn")
            ready(page)
            expect(
                page.locator(".atlas-code-notice").filter(
                    has_text="model_calls measured 1"
                )
            ).to_contain_text("limit 1")
            snapshot(page, "honesty-limit")
            for name, values in (
                ("flag-off", {"flag": "0"}),
                ("guest", {"flag": "1", "stable": "1"}),
            ):
                mode(page, **values)
                page.reload(wait_until="networkidle")
                expect(page.locator("#atlas-code-entry")).to_have_count(0)
                expect(page.locator("#atlas-code-panel")).to_have_count(0)
                assert page.request.get(URL + "/atlas-code/code/ui").status == 404
                page.screenshot(path=str(OUTPUT / f"{name}.png"))
            mode(page)
            (OUTPUT / "replay-evidence.json").write_text(
                json.dumps(
                    {
                        "verified": [
                            "entry",
                            "flag-off",
                            "guest",
                            "archive",
                            "activity-output",
                            "plan",
                            "diff",
                            "allow",
                            "deny",
                            "stop",
                            "queue",
                            "recap",
                            "exports",
                            "copy",
                            "reload",
                            "honesty",
                            "limit",
                        ],
                        "pipeline": evidence,
                        "page_errors": errors,
                        "timestamp": time.time(),
                    },
                    indent=2,
                )
            )
        assert not errors, errors
        browser.close()
        print("Code UI", "live" if LIVE else "replay", "real-page checks completed")


if __name__ == "__main__":
    main()
