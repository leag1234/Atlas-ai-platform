"""Browser security suite for isolated Atlas artifact previews."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Browser, BrowserContext, Page, Response, sync_playwright

from services.orchestrator.artifact_routes import PREVIEW_CSP
from services.orchestrator.artifact_tokens import issue_token


BASE_URL = os.environ.get("ATLAS_SECURITY_BASE_URL", "").rstrip("/")
PROXY_SECRET = os.environ.get("ATLAS_SECURITY_PROXY_SECRET", "")
TRUSTED_HEADERS = {
    "X-Atlas-User": "artifact-security-owner@example.test",
    "X-Atlas-Proxy-Secret": PROXY_SECRET,
}
FIXTURES = Path(__file__).parent / "fixtures" / "artifacts"
EXPECTED_CSP = PREVIEW_CSP + "'self'"
EXPECTED_ACCESS = {
    "cookie": "SecurityError",
    "localStorage": "null",
    "parentDocument": "SecurityError",
    "parentLocalStorage": "SecurityError",
}


class AttackerServer:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with server.lock:
                    server.hits[self.path] += 1
                self.send_response(204)
                self.end_headers()

            def do_POST(self) -> None:
                with server.lock:
                    server.hits[self.path] += 1
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def count(self, path: str) -> int:
        with self.lock:
            return self.hits[path]

    @property
    def total(self) -> int:
        with self.lock:
            return sum(self.hits.values())

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


def seed(name: str, attacker: str) -> dict[str, int | str]:
    content = (
        (FIXTURES / name)
        .read_bytes()
        .replace(b"__ATTACKER__", attacker.encode("ascii"))
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="atlas-security-", suffix=".html"
    )
    temporary_path = Path(temporary_name)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
    try:
        environment = os.environ.copy()
        environment["ATLAS_ARTIFACT_OWNER"] = TRUSTED_HEADERS["X-Atlas-User"]
        result = subprocess.run(
            [
                "python3",
                "-m",
                "services.orchestrator.artifact_store",
                "seed",
                str(temporary_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )
        artifact_id, version = result.stdout.split()
        return {"id": artifact_id, "version": int(version), "filename": name}
    finally:
        temporary_path.unlink()


def open_link(artifact: dict[str, int | str], title: str) -> str:
    artifact_id = str(artifact["id"])
    version = int(artifact["version"])
    return (
        f"/api/atlas/artifacts/{artifact_id}/versions/{version}/open"
        + f"#atlas-view=app&artifact={artifact_id}&version={version}&title={title}"
    )


def open_side_view(page: Page, artifact: dict[str, int | str], title: str) -> None:
    response = page.goto(BASE_URL + open_link(artifact, title), wait_until="load")
    assert response is not None and response.status == 303, response and response.status
    page.locator(".atlas-side-frame").wait_for(timeout=10000)
    page.locator(".atlas-side-frame").wait_for(timeout=10000)


def frame_state(page: Page) -> dict[str, Any]:
    frame = page.frames[-1]
    state: dict[str, Any] = frame.evaluate("window.__atlasSecurity")
    return state


def assert_no_attacker_hits(attacker: AttackerServer, label: str) -> None:
    assert attacker.total == 0, f"{label}: attacker hits {dict(attacker.hits)}"


def assert_network_security(page: Page, attacker: AttackerServer) -> None:
    parent_url = page.url
    page.wait_for_timeout(3000)
    state = frame_state(page)
    assert state["origin"] == "null", state
    assert state["access"] == EXPECTED_ACCESS, state
    assert state["serviceWorker"] is True, state
    assert_no_attacker_hits(attacker, "network and storage")
    assert page.url == parent_url, (parent_url, page.url)


def click_network_controls(page: Page, attacker: AttackerServer) -> None:
    frame = page.frames[-1]
    frame.locator("#submit-form").click(timeout=2000)
    frame.locator("#top-link").click(timeout=2000)
    frame.locator("#blank-link").click(timeout=2000)
    page.wait_for_timeout(500)
    assert len(page.context.pages) == 1, "a popup page was opened"
    assert_no_attacker_hits(attacker, "form, top navigation, and popup")


def assert_preview_response(response: Response) -> None:
    assert response.status == 200, response.status
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.from_service_worker is False


def open_in_tab(
    context: BrowserContext, page: Page, artifact: dict[str, int | str]
) -> Page:
    open_url = BASE_URL + open_link(artifact, "Tab probe").split("#", 1)[0]
    with context.expect_page() as tab_info:
        page.evaluate("url => window.open(url, '_blank', 'noopener')", open_url)
    tab = tab_info.value
    response = tab.goto(open_url, wait_until="load")
    assert response is not None
    assert_preview_response(response)
    assert tab.evaluate("self.origin") == "null"
    assert (
        tab.evaluate(
            """() => {
          try { document.cookie; return "no-error"; }
          catch (error) { return error.name; }
        }"""
        )
        == "SecurityError"
    )
    return tab


def assert_tab_network(tab: Page, attacker: AttackerServer) -> None:
    tab.evaluate(
        """async attacker => {
          const attempts = [];
          const record = (name, promise) => {
            attempts.push(name);
            Promise.resolve(promise).catch(() => {});
          };
          record("fetch", fetch(attacker + "/tab-fetch", {credentials: "include"}));
          try { top.location = attacker + "/tab-top"; } catch (error) {}
          try { const popup = window.open(attacker + "/tab-popup"); if (popup) attempts.push("popup"); }
          catch (error) {}
          window.__atlasTabSecurity = attempts;
        }""",
        attacker.url,
    )
    tab.wait_for_timeout(500)
    assert_no_attacker_hits(attacker, "open-in-tab network")
    assert len(tab.context.pages) == 2, "a tab popup was opened"


def assert_refusals(page: Page, artifact: dict[str, int | str]) -> None:
    artifact_id = str(artifact["id"])
    version = int(artifact["version"])
    filename = str(artifact["filename"])
    valid_token = issue_token(artifact_id, version)
    forged_token = valid_token[:-1] + ("A" if valid_token[-1] != "A" else "B")
    expired_token = issue_token(artifact_id, version, now=int(time.time()) - 901)
    paths = [
        f"/api/atlas/preview/{valid_token}/{quote(filename, safe='')}/undeclared.html",
        f"/api/atlas/preview/{expired_token}/{quote(filename, safe='')}",
        f"/api/atlas/preview/{forged_token}/{quote(filename, safe='')}",
    ]
    for path in paths:
        response = page.goto(BASE_URL + path)
        assert response is not None
        assert response.status == 404, (path, response.status)
        assert response.headers["content-security-policy"] == EXPECTED_CSP


def assert_stopped_notice(page: Page, attacker: AttackerServer, path: str) -> None:
    page.wait_for_timeout(2000)
    assert attacker.count(path) == 1, (path, dict(attacker.hits))
    assert page.locator(".atlas-side-stopped").is_visible()


def assert_storage_shim(page: Page, artifact: dict[str, int | str]) -> None:
    frame = page.frames[-1]
    expect_heading(frame)
    assert frame.locator("body").get_attribute("data-score") == "42"
    page.get_by_role("button", name="Restart app").click()
    restarted_frame = page.frame_locator(".atlas-side-frame")
    restarted_frame.locator("h1").wait_for(timeout=5000)
    assert restarted_frame.locator("body").get_attribute("data-score") is None


def expect_heading(frame: Any) -> None:
    frame.locator("h1").wait_for(timeout=5000)


def run_engine(browser: Browser, attacker: AttackerServer, engine: str) -> None:
    attacker.reset()
    context = browser.new_context(viewport={"width": 1500, "height": 1000})
    page = context.new_page()
    page.goto(BASE_URL, wait_until="load")

    network = seed("hostile-network.html", attacker.url)
    open_side_view(page, network, "Network probe")
    assert_network_security(page, attacker)
    click_network_controls(page, attacker)

    tab = open_in_tab(context, page, network)
    assert_tab_network(tab, attacker)
    tab.close()

    assert_refusals(page, network)
    page.goto(BASE_URL, wait_until="load")

    navigation = seed("hostile-navigation.html", attacker.url)
    open_side_view(page, navigation, "Navigation probe")
    assert_stopped_notice(page, attacker, "/self-navigation")
    page.evaluate("window.atlasSideView.close()")

    refresh = seed("hostile-refresh.html", attacker.url)
    open_side_view(page, refresh, "Refresh probe")
    assert_stopped_notice(page, attacker, "/meta-refresh")
    page.evaluate("window.atlasSideView.close()")

    storage = seed("hostile-storage.html", attacker.url)
    open_side_view(page, storage, "Storage probe")
    assert_storage_shim(page, storage)
    page.evaluate("window.atlasSideView.close()")

    context.close()
    print(f"PASS {engine}: S1-S12 mode (a); S13 not run (T12 absent)")


def engine_available(playwright: Any, engine: str) -> bool:
    try:
        executable = getattr(playwright, engine).executable_path
        return bool(executable and Path(executable).exists())
    except Exception:
        return False


def main() -> None:
    if not BASE_URL or not PROXY_SECRET:
        print(
            "SKIP: ATLAS_SECURITY_BASE_URL and ATLAS_SECURITY_PROXY_SECRET are required"
        )
        return
    attacker = AttackerServer()
    attacker.start()
    try:
        with sync_playwright() as playwright:
            engines = [
                engine
                for engine in ("chromium", "firefox", "webkit")
                if engine_available(playwright, engine)
            ]
            if not engines:
                raise SystemExit("no Playwright browser engine is installed")
            print("Engines: " + ", ".join(engines))
            for engine in engines:
                browser = getattr(playwright, engine).launch(
                    headless=True,
                    args=["--no-sandbox"] if engine == "chromium" else [],
                )
                try:
                    run_engine(browser, attacker, engine)
                finally:
                    browser.close()
    finally:
        attacker.stop()


if __name__ == "__main__":
    main()
