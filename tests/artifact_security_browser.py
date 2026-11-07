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
from typing import Any, Callable, cast
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
    artifact_id = str(artifact["id"])
    version = int(artifact["version"])
    src = f"/api/atlas/artifacts/{artifact_id}/versions/{version}/open"
    descriptor = {
        "kind": "app",
        "artifact": artifact_id,
        "version": version,
        "src": src,
        "download": f"/api/atlas/artifacts/{artifact_id}/versions/{version}/download",
        "title": title,
        "slot": "top",
    }
    page.evaluate("descriptor => window.atlasSideView.open(descriptor)", descriptor)
    frame = page.frame_locator(".atlas-side-frame")
    frame.locator("#marker").wait_for(timeout=10000)


def run_fixture(page: Page) -> None:
    frame = page.frame_locator(".atlas-side-frame")
    frame.locator("#run").click(timeout=5000)
    frame.locator("#result").wait_for(timeout=5000)


def frame_result(page: Page) -> str:
    return page.frame_locator(".atlas-side-frame").locator("#result").inner_text()


def frame_state(page: Page) -> dict[str, Any]:
    state = (
        page.frame_locator(".atlas-side-frame")
        .locator("#result")
        .evaluate("() => window.__atlasSecurity")
    )
    return cast(dict[str, Any], state)


def assert_no_attacker_hits(attacker: AttackerServer, label: str) -> None:
    assert attacker.total == 0, f"{label}: attacker hits {dict(attacker.hits)}"


def assert_network_security(page: Page, attacker: AttackerServer) -> None:
    parent_url = page.url
    run_fixture(page)
    page.wait_for_timeout(3000)
    state = frame_state(page)
    assert state["origin"] == "null", state
    assert state["access"] == EXPECTED_ACCESS, state
    assert state["serviceWorker"] is True, state
    assert frame_result(page) == "blocked:", frame_result(page)
    assert_no_attacker_hits(attacker, "network and storage")
    assert page.url == parent_url, (parent_url, page.url)


def click_network_controls(page: Page, attacker: AttackerServer) -> None:
    frame = page.frame_locator(".atlas-side-frame")
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
    tab.locator("#marker").wait_for(timeout=5000)
    tab.locator("#run").click(timeout=5000)
    tab.locator("#result").wait_for(timeout=5000)
    tab.wait_for_timeout(500)
    result = tab.locator("#result").inner_text()
    assert result == "blocked:", result
    state = tab.evaluate("window.__atlasSecurity")
    assert state["access"] == EXPECTED_ACCESS, state
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
    run_fixture(page)
    assert frame_result(page) == "allowed", frame_result(page)
    page.wait_for_timeout(2000)
    assert attacker.count(path) == 1, (path, dict(attacker.hits))
    stopped = page.locator(".atlas-side-stopped")
    stopped.wait_for(timeout=5000)
    assert stopped.is_visible()


def assert_storage_shim(page: Page, artifact: dict[str, int | str]) -> None:
    frame = page.frame_locator(".atlas-side-frame")
    expect_heading(frame)
    run_fixture(page)
    assert frame.locator("body").get_attribute("data-score") == "42"
    assert frame_result(page) == "allowed", frame_result(page)
    page.get_by_role("button", name="Restart app").click()
    restarted_frame = page.frame_locator(".atlas-side-frame")
    restarted_frame.locator("h1").wait_for(timeout=5000)
    assert restarted_frame.locator("body").get_attribute("data-score") is None


def expect_heading(frame: Any) -> None:
    frame.locator("h1").wait_for(timeout=5000)


def run_network_case(
    page: Page, attacker: AttackerServer, network: dict[str, int | str]
) -> None:
    open_side_view(page, network, "Network probe")
    assert_network_security(page, attacker)
    click_network_controls(page, attacker)


def run_tab_case(
    context: BrowserContext,
    page: Page,
    attacker: AttackerServer,
    network: dict[str, int | str],
) -> None:
    tab = open_in_tab(context, page, network)
    assert_tab_network(tab, attacker)
    tab.close()


def run_refusals_case(page: Page, network: dict[str, int | str]) -> None:
    assert_refusals(page, network)
    page.goto(BASE_URL, wait_until="load")


def run_stopped_case(
    page: Page,
    attacker: AttackerServer,
    artifact: dict[str, int | str],
    title: str,
    path: str,
) -> None:
    open_side_view(page, artifact, title)
    assert_stopped_notice(page, attacker, path)
    page.evaluate("window.atlasSideView.close()")


def run_storage_case(page: Page, storage: dict[str, int | str]) -> None:
    open_side_view(page, storage, "Storage probe")
    assert_storage_shim(page, storage)
    page.evaluate("window.atlasSideView.close()")


def run_engine(
    browser: Browser,
    attacker: AttackerServer,
    engine: str,
    failures: list[tuple[str, BaseException]],
) -> None:
    attacker.reset()
    context = browser.new_context(
        viewport={"width": 1500, "height": 1000},
        extra_http_headers=TRUSTED_HEADERS,
    )
    page = context.new_page()
    response = page.request.post(
        BASE_URL + "/api/v1/auths/signin",
        headers=TRUSTED_HEADERS,
        data={"email": TRUSTED_HEADERS["X-Atlas-User"], "password": ""},
    )
    assert response.ok, (response.status, response.text())
    page.goto(BASE_URL, wait_until="load")

    cases: list[tuple[str, Callable[[], None]]] = []

    def case(name: str, run: Callable[[], None]) -> None:
        cases.append((name, run))

    network = seed("hostile-network.html", attacker.url)
    case(
        "S1-S12 mode (a)",
        lambda: run_network_case(page, attacker, network),
    )
    case(
        "open in a tab",
        lambda: run_tab_case(context, page, attacker, network),
    )
    case(
        "preview refusals",
        lambda: run_refusals_case(page, network),
    )

    navigation = seed("hostile-navigation.html", attacker.url)
    case(
        "self-navigation stop",
        lambda: run_stopped_case(
            page, attacker, navigation, "Navigation probe", "/self-navigation"
        ),
    )

    refresh = seed("hostile-refresh.html", attacker.url)
    case(
        "meta-refresh stop",
        lambda: run_stopped_case(
            page, attacker, refresh, "Refresh probe", "/meta-refresh"
        ),
    )

    about_blank = seed("hostile-about-blank.html", attacker.url)
    case(
        "about:blank navigation stop",
        lambda: run_stopped_case(
            page, attacker, about_blank, "About blank probe", "/about-blank-navigation"
        ),
    )

    storage = seed("hostile-storage.html", attacker.url)
    case(
        "storage shim",
        lambda: run_storage_case(page, storage),
    )

    try:
        for name, run in cases:
            try:
                run()
                print(f"PASS {engine}: {name}")
            except BaseException as error:
                failures.append((f"{engine}: {name}", error))
                print(f"FAIL {engine}: {name}: {error}")
    finally:
        context.close()


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
            failures: list[tuple[str, BaseException]] = []
            for engine in engines:
                browser = getattr(playwright, engine).launch(
                    headless=True,
                    args=["--no-sandbox"] if engine == "chromium" else [],
                )
                try:
                    run_engine(browser, attacker, engine, failures)
                except BaseException as error:
                    failures.append((engine, error))
                    print(f"FAIL {engine}: {error}")
                finally:
                    browser.close()
            if failures:
                raise SystemExit(1)
    finally:
        attacker.stop()


if __name__ == "__main__":
    main()
