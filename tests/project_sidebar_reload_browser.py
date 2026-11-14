"""Sidebar project list across reloads, on a disposable Open WebUI of either release.

Open WebUI runs from its pinned image (or ATLAS_WEBUI_IMAGE, for example v0.13.3) with the
settings of infra/chat-ui.env that concern the interface, and the Atlas scripts mounted as on an
instance. The Atlas project API is answered by the browser with synthetic data (two projects),
so no provider, no project database and no credit is used. Checks, in light, dark and phone views:
the "Projects" list is in the sidebar on an ordinary chat page after a load and after a reload,
it stays after leaving the project view, a row opens its project, and the user menu offers no
status entry and no count of signed-in users. Screenshots go to BRAIN/project-sidebar-reload/<release>.

Run from the repository: PYTHONPATH=.:services/model-gateway python tests/project_sidebar_reload_browser.py
(ATLAS_WEBUI_IMAGE selects the release, ATLAS_SIDEBAR_MODES the views).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import Page, Route, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.orchestrator.serving import WEBUI_IMAGE  # noqa: E402

PORT = 17952
NAME = f"atlas-sidebar-reload-{PORT}"
URL = f"http://127.0.0.1:{PORT}"
RELEASE = re.search(r":(v[0-9.]+)@", WEBUI_IMAGE)
OUTPUT = (
    ROOT / "BRAIN/project-sidebar-reload" / (RELEASE.group(1) if RELEASE else "image")
)
ORG = "0b5d6a8e-1c2f-4e3a-9b7c-2d4e6f8a0c11"
NAMES = {
    "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e51": ("Projet Orion", "2026-11-14T10:00:00Z"),
    "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e52": ("Budget 2027", "2026-11-13T10:00:00Z"),
}
PROJECTS: list[dict[str, object]] = [
    {
        "id": project,
        "name": name,
        "updated_at": updated,
        "org_id": ORG,
        "revision": 1,
        "role": "owner",
        "state": "active",
        "hidden": False,
        "instructions": "",
        "default_action": "chat",
        "created_at": "2026-11-01T10:00:00Z",
    }
    for project, (name, updated) in NAMES.items()
]
SECOND = "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e52"
# Settings of infra/chat-ui.env that shape the interface (the provider and terminal ones do not apply).
INTERFACE_SETTINGS = (
    "WEBUI_NAME",
    "ENABLE_VERSION_UPDATE_CHECK",
    "ENABLE_EVALUATION_ARENA_MODELS",
    "ENABLE_COMMUNITY_SHARING",
    "ENABLE_PERSISTENT_CONFIG",
    "UI_VISIBILITY",
    "ENABLE_NOTIFICATIONS",
    "ENABLE_USER_STATUS",
    "ENABLE_PUBLIC_ACTIVE_USERS_COUNT",
)


def settings() -> dict[str, str]:
    values = {}
    for line in (ROOT / "infra/chat-ui.env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key in INTERFACE_SETTINGS:
            values[key] = value
    return values


@contextmanager
def open_webui() -> Iterator[None]:
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", PORT))
    environment = {
        "PORT": str(PORT),
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER": "X-Atlas-User-Role",
        "DEFAULT_USER_ROLE": "user",
        "ENABLE_SIGNUP": "False",
        "ENABLE_OLLAMA_API": "False",
        "ENABLE_OPENAI_API": "False",
        "OFFLINE_MODE": "True",
        "HF_HUB_OFFLINE": "1",
        "DEFAULT_LOCALE": "",
        # As services/orchestrator/serving.py starts an instance.
        "ENABLE_MEMORIES": "False",
        "ENABLE_CALENDAR": "False",
        "ENABLE_AUTOMATIONS": "False",
        "ENABLE_NOTES": "False",
        "ENABLE_EASTER_EGGS": "False",
        **settings(),
    }
    options = ["run", "-d", "--rm", "--name", NAME, "--network", "host"]
    for key, value in environment.items():
        options += ["-e", f"{key}={value}"]
    mounts = [
        ("assets/brand", "brand"),
        ("services/orchestrator/static/fonts", "fonts"),
        ("services/orchestrator/static/atlas-version.json", "atlas-version.json"),
    ] + [
        ("services/orchestrator/" + name, name)
        for name in (
            "chat-ui.js",
            "guest-chat.js",
            "chat-ui.css",
            "brand-ui.js",
            "settings-ui.js",
            "settings-locales.json",
        )
    ]
    for source, target in mounts:
        options += [
            "-v",
            f"{ROOT / source}:/app/backend/open_webui/static/atlas/{target}:ro",
        ]
    subprocess.run(["docker", *options, WEBUI_IMAGE], check=True, capture_output=True)
    try:
        # The instance shell: the same tags as an Atlas instance (services/orchestrator/ui_injection.py).
        from services.orchestrator.ui_injection import ATLAS_UI_TAGS

        tags = ATLAS_UI_TAGS.decode()
        script = (
            "from pathlib import Path; p=Path('/app/build/index.html'); "
            f"p.write_text(p.read_text().replace('</head>', {tags!r} + '</head>'))"
        )
        subprocess.run(["docker", "exec", NAME, "python", "-c", script], check=True)
        for _ in range(180):
            try:
                with urllib.request.urlopen(URL + "/health", timeout=2) as reply:
                    if reply.status == 200:
                        break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("disposable_ui_not_ready")
        # The first account is the administrator; the checked accounts are ordinary users.
        request = urllib.request.Request(
            URL + "/api/v1/auths/signin",
            data=b'{"email": "admin@localhost", "password": ""}',
            headers={
                "Content-Type": "application/json",
                "X-Atlas-User": "admin@localhost",
                "X-Atlas-User-Role": "admin",
            },
        )
        urllib.request.urlopen(request, timeout=30).read()
        yield
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)


def atlas_api(route: Route) -> None:
    """Synthetic Atlas API: the signed-in user belongs to one organization with two projects."""
    request = route.request
    path = request.url.split("?", 1)[0].split(URL, 1)[1]
    asset = {
        "/project-mode/style.css": ("project_mode.css", "text/css"),
        "/project-mode/locale.js": ("project_locale.js", "text/javascript"),
        "/project-mode/client.js": ("project_mode.js", "text/javascript"),
    }.get(path)
    if asset:
        source = (
            os.environ.get("ATLAS_PROJECT_CLIENT")
            if asset[0] == "project_mode.js"
            else None
        )
        body = Path(source or ROOT / "services/orchestrator" / asset[0]).read_text()
        route.fulfill(status=200, content_type=asset[1], body=body)
        return
    if path.endswith("/client.js") or path.endswith("/style.css"):
        route.fulfill(status=200, content_type="text/javascript", body="")
        return
    projects = f"/api/atlas/orgs/{ORG}/projects"
    value: object
    if path == "/api/atlas/me":
        value = {"memberships": [{"org_id": ORG, "role": "member", "name": "Atlas"}]}
    elif path == projects:
        value = {"items": PROJECTS, "next_cursor": None}
    elif path.startswith(projects + "/") and path.count("/") == 6:
        value = next(item for item in PROJECTS if path.endswith(str(item["id"])))
    elif request.method == "GET" and path.startswith(projects + "/"):
        value = {"items": [], "next_cursor": None, "content": ""}
    else:
        route.fulfill(
            status=404, content_type="application/json", body='{"error":"not_found"}'
        )
        return
    route.fulfill(status=200, content_type="application/json", body=json.dumps(value))


def sidebar(page: Page, phone: bool) -> None:
    """Shows the sidebar as a user does (a phone starts with it closed)."""
    shown = page.evaluate(
        "() => { const s = document.getElementById('sidebar'); return !!s && s.getAttribute('aria-hidden') !== 'true' && s.getBoundingClientRect().right > 0; }"
    )
    if not phone or shown:
        return
    page.locator(
        "#sidebar-toggle-button:visible, #sidebar-open-button:visible"
    ).first.click()
    page.wait_for_timeout(800)


def listed(page: Page) -> list[str]:
    return page.locator(
        "#atlas-project-sidebar-list [data-sidebar-project]"
    ).all_inner_texts()


def home(page: Page, url: str | None = None) -> None:
    """Waits for the composer; a fresh instance can answer its very first page with a 500, so a
    page opened by address is loaded a second time once if the composer does not come."""
    for attempt in (1, 2):
        if url:
            page.goto(url, wait_until="domcontentloaded")
        try:
            page.locator("#chat-input").wait_for(timeout=60000)
            break
        except Exception:  # noqa: BLE001
            if attempt == 2 or not url:
                raise
            print("INFO first page load retried", flush=True)
    page.wait_for_timeout(1500)


def check(results: list[str], name: str, passed: bool, detail: str = "") -> None:
    results.append(
        ("PASS " if passed else "FAIL ") + name + (" - " + detail if detail else "")
    )
    print(results[-1], flush=True)


def journey(page: Page, mode: str, results: list[str]) -> None:
    phone = mode == "phone"

    def shot(name: str) -> None:
        page.screenshot(path=str(OUTPUT / f"{mode}-{name}.png"))

    home(page, URL + "/")
    sidebar(page, phone)
    expected = [name for name, _ in NAMES.values()]
    expect(page.locator("#atlas-project-sidebar-list")).to_be_visible(timeout=10000)
    check(
        results,
        f"{mode} first load lists the projects",
        listed(page) == expected,
        str(listed(page)),
    )
    shot("first-load")
    for attempt in (1, 2):
        page.reload(wait_until="domcontentloaded")
        home(page)
        sidebar(page, phone)
        page.wait_for_timeout(1500)
        check(
            results,
            f"{mode} reload {attempt} keeps the projects",
            listed(page) == expected,
            str(listed(page)),
        )
    shot("after-reload")
    # Into the project view and back to an ordinary chat: the list stays.
    page.locator("#atlas-project-sidebar-list .atlas-project-sidebar-all").click()
    expect(page.locator("#atlas-project")).to_be_visible(timeout=10000)
    page.wait_for_timeout(800)
    if phone:
        page.locator(".atlas-project-sidebar-open").click()
        page.wait_for_timeout(800)
    page.locator("a#sidebar-new-chat-button:visible").first.click()
    page.wait_for_timeout(1500)
    sidebar(page, phone)
    gone = page.locator("#atlas-project").count() == 0
    check(
        results,
        f"{mode} back to a chat keeps the projects",
        gone and listed(page) == expected,
        f"view closed={gone} {listed(page)}",
    )
    shot("after-project-view")
    # A row opens its project from an ordinary chat page.
    page.locator(
        "#atlas-project-sidebar-list [data-sidebar-project]", has_text="Budget 2027"
    ).click()
    expect(page.locator("#atlas-project")).to_be_visible(timeout=10000)
    page.wait_for_timeout(1500)
    opened = "project=" + SECOND in page.url
    check(results, f"{mode} a sidebar row opens its project", opened, page.url[-60:])
    shot("project-opened")
    # A project address reloaded: the list is there with the open project marked.
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#atlas-project")).to_be_visible(timeout=60000)
    page.wait_for_timeout(2500)
    if phone:
        page.locator(".atlas-project-sidebar-open").click()
        page.wait_for_timeout(800)
    current = page.locator(
        "#atlas-project-sidebar-list [aria-current]"
    ).all_inner_texts()
    check(
        results,
        f"{mode} project address reload marks the project",
        current == ["Budget 2027"],
        str(current),
    )
    shot("project-reload")


def user_menu(page: Page, mode: str, results: list[str]) -> None:
    home(page, URL + "/")
    sidebar(page, mode == "phone")
    page.locator("#sidebar").get_by_text(re.compile(r"@localhost")).last.click()
    page.wait_for_timeout(1000)
    menu = page.locator("[role=menu]:visible, .app-dropdown-menu:visible").last
    text = menu.inner_text() if menu.count() else ""
    page.screenshot(path=str(OUTPUT / f"{mode}-user-menu.png"))
    status = re.search(r"statut|status", text, re.I) is not None
    check(
        results,
        f"{mode} user menu has no status entry",
        bool(text) and not status,
        text.replace("\n", " | ")[:200],
    )
    # The count of signed-in users beside the address (a green dot and a number) is for administrators only.
    count = re.search(r"^\d+$", text, re.M) is not None
    check(
        results,
        f"{mode} user menu shows no active user count",
        bool(text) and not count,
    )
    page.keyboard.press("Escape")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    modes = os.environ.get("ATLAS_SIDEBAR_MODES", "light,dark,phone").split(",")
    with open_webui(), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for mode in modes:
            phone = mode == "phone"
            context = browser.new_context(
                locale="fr-FR",
                viewport={"width": 390, "height": 844}
                if phone
                else {"width": 1440, "height": 900},
                is_mobile=phone,
                has_touch=phone,
                extra_http_headers={
                    "X-Atlas-User": mode + "@localhost",
                    "X-Atlas-User-Role": "user",
                },
            )
            context.add_init_script(
                "localStorage.setItem('locale','fr-FR'); localStorage.setItem('theme',"
                + json.dumps("dark" if mode == "dark" else "light")
                + ");"
            )
            context.route(re.compile(r".*/(api/atlas|project-mode)/.*"), atlas_api)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)[:200]))
            for step in (journey, user_menu):
                try:
                    step(page, mode, results)
                except Exception as exc:  # noqa: BLE001
                    check(
                        results,
                        f"{mode} {step.__name__}",
                        False,
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                    page.screenshot(
                        path=str(OUTPUT / f"{mode}-{step.__name__}-failure.png")
                    )
            check(results, f"{mode} no page error", not errors, str(errors[:3]))
            context.close()
        browser.close()
    failed = [line for line in results if line.startswith("FAIL")]
    print(
        f"{len(results) - len(failed)} of {len(results)} checks pass on {WEBUI_IMAGE.split('@')[0]}"
    )
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
