"""Browser controls against the real store/API with a test-only session boundary."""

import json
import os
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import Route, sync_playwright

from packages.user_memory import MemoryWrite
from services.retrieval.api import app as retrieval_app
from services.retrieval.user_memory import UserMemory


def main() -> None:
    with tempfile.TemporaryDirectory(dir="BRAIN") as directory:
        path = Path(directory) / "memory.sqlite"
        memory = UserMemory(path)
        memory.write(
            "stable",
            "alex-fictional",
            MemoryWrite(text="I prefer tea", kind="preference"),
        )
        memory.write(
            "stable",
            "alex-fictional",
            MemoryWrite(text="I work in Lyon", kind="context"),
        )
        secret = secrets.token_hex(32)
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_USER_MEMORY_DB": str(path),
                    "ATLAS_USER_MEMORY_SECRET": secret,
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_MEMORY_ENABLED": "1",
                },
            ),
            TestClient(retrieval_app) as api,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def route(request: Route) -> None:
                    suffix = request.request.url.split("/api/atlas/user-memory", 1)[1]
                    reply = api.request(
                        request.request.method,
                        "/internal/atlas/user-memory" + suffix,
                        content=request.request.post_data,
                        headers={
                            "X-Atlas-Memory-Secret": secret,
                            "X-Atlas-Memory-Subject": "alex-fictional",
                            "Content-Type": "application/json",
                        },
                    )
                    request.fulfill(
                        status=reply.status_code,
                        body=reply.text,
                        content_type="application/json",
                    )

                page.route("http://memory.test/api/atlas/user-memory**", route)
                page.route(
                    "http://memory.test/",
                    lambda route: route.fulfill(
                        body="<html><body>Atlas</body></html>", content_type="text/html"
                    ),
                )
                page.goto("http://memory.test/")
                page.add_script_tag(path="services/orchestrator/user_memory.js")
                page.get_by_role("button", name="Memory", exact=True).click()
                page.get_by_label("Memory text").first.wait_for()
                assert page.get_by_label("Use personal memory").is_checked()
                page.get_by_label("Memory text").first.fill("I prefer coffee")
                page.get_by_role("button", name="Save", exact=True).first.click()
                page.wait_for_function(
                    "Array.from(document.querySelectorAll('textarea')).some(e => e.value === 'I prefer coffee')"
                )
                assert (
                    memory.view("stable", "alex-fictional")["entries"][-1]["text"]
                    == "I prefer coffee"
                )
                page.get_by_label("Use personal memory").uncheck()
                page.wait_for_function(
                    "Array.from(document.querySelectorAll('button')).some(e => e.textContent === 'Save' && e.disabled)"
                )
                assert not memory.view("stable", "alex-fictional")["effective_enabled"]
                page.get_by_label("Use personal memory").check()
                page.get_by_role("button", name="Delete", exact=True).first.click()
                page.wait_for_function(
                    "document.querySelectorAll('textarea').length === 1"
                )
                assert len(memory.view("stable", "alex-fictional")["entries"]) == 1
                page.on("dialog", lambda dialog: dialog.accept())
                page.get_by_role("button", name="Delete all memories").click()
                page.get_by_text("No saved memories.", exact=True).wait_for()
                assert memory.view("stable", "alex-fictional")["entries"] == []
                page.screenshot(path="BRAIN/user-memory/ui.png")
                print(
                    json.dumps(
                        dict(
                            browser="chromium", list_edit_toggle_delete_delete_all=True
                        )
                    )
                )
            finally:
                browser.close()


if __name__ == "__main__":
    main()
