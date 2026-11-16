"""Playwright check: phone chat header toggle and answer heading sizes (owner report 2026-11-16).

On a 390 px phone the sidebar toggle was a 28 px box with a 16 px grey icon, and an answer's H1
(1.75x body, 1.6 leading) wrapped a short title over three to four lines. This check asserts, in
light and dark themes, on Open WebUI 0.11 and 0.13 alike:

- phone (390 x 844): the navbar #sidebar-toggle-button is at least 44 x 44 px, its icon at least
  22 px, drawn in the body text colour, and it overlaps no other header control;
- phone: assistant h1 <= 1.25x, h2 <= 1.125x, h3 <= 1.0625x the body size, with h1 > h2 > h3 > body,
  a heading leading of at most 1.3, and the test title on at most two lines;
- tablet (768 x 1024) and desktop (1440 x 900): the desktop scale (h1 1.75x, h2 1.375x, h3 1.125x).

Target: ATLAS_PHONE_UI_URL (default http://127.0.0.1:3391, a disposable Open WebUI). Sign-in:
with ATLAS_PHONE_UI_BASIC_USER and ATLAS_PHONE_UI_BASIC_FILE (a file holding the password, never
printed) the page opens /?connexion with HTTP credentials, as on the staging instance; otherwise a
disposable account is created through the API. The check writes one chat through the chat API and
deletes it at the end; it calls no model.

By default the repository stylesheet services/orchestrator/chat-ui.css is injected, so a vanilla
Open WebUI checks the branch; ATLAS_PHONE_UI_INJECT=0 checks the stylesheet the instance serves
(after a deploy). Screenshots go to ATLAS_PHONE_UI_OUTPUT (default BRAIN/phone-ui).
"""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import APIRequestContext, Browser, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

URL = os.environ.get("ATLAS_PHONE_UI_URL", "http://127.0.0.1:3391").rstrip("/")
OUTPUT = Path(os.environ.get("ATLAS_PHONE_UI_OUTPUT", "BRAIN/phone-ui"))
INJECT = os.environ.get("ATLAS_PHONE_UI_INJECT", "1") != "0"
BASIC_USER = os.environ.get("ATLAS_PHONE_UI_BASIC_USER", "")
BASIC_FILE = os.environ.get("ATLAS_PHONE_UI_BASIC_FILE", "")
EMAIL = os.environ.get("ATLAS_PHONE_UI_EMAIL", "phone-ui@example.test")
PASSWORD = os.environ.get("ATLAS_PHONE_UI_PASSWORD", "local-disposable-password")
CSS = Path("services/orchestrator/chat-ui.css")
RUN_TIMEOUT_SECONDS = 300
TITLE = "Le compostage en appartement : la fiche pratique"
ANSWER = (
    f"# {TITLE}\n\n## Choisir sa méthode\n\nTrois solutions selon votre espace.\n\n"
    "## Entretenir son compost au quotidien\n\n### Les règles d'or\n\n"
    "- Alternez déchets humides et matières sèches.\n"
)
VIEWPORTS = (("phone", 390, 844), ("tablet", 768, 1024), ("desktop", 1440, 900))
DESKTOP_SCALE = {"H1": 1.75, "H2": 1.375, "H3": 1.125}
PHONE_LIMIT = {"H1": 1.25, "H2": 1.125, "H3": 1.0625}

MEASURE = """() => {
  const rect = node => { const r = node.getBoundingClientRect(); return {left: r.left, top: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom}; };
  const toggle = [...document.querySelectorAll('#sidebar-toggle-button')]
    .find(node => !node.closest('#sidebar') && node.getBoundingClientRect().width > 0);
  const answer = [...document.querySelectorAll('.chat-assistant .markdown-prose')].pop();
  const paragraph = answer && answer.querySelector('p');
  const controls = [...document.querySelectorAll('header button, nav button, button[aria-label], a[aria-label]')]
    .filter(node => node !== toggle && !node.closest('#sidebar,[data-sonner-toaster]') && !(toggle && toggle.contains(node)))
    .map(node => rect(node)).filter(box => box.width > 0 && box.top >= 0 && box.top < 70);
  return {
    toggle: toggle ? {box: rect(toggle), icon: rect(toggle.querySelector('svg')), color: getComputedStyle(toggle).color} : null,
    controls,
    toasts: [...document.querySelectorAll('[data-sonner-toast]')].map(node => node.innerText.trim()).filter(Boolean),
    body: paragraph ? {size: parseFloat(getComputedStyle(paragraph).fontSize), color: getComputedStyle(paragraph).color} : null,
    headings: answer ? [...answer.querySelectorAll('h1,h2,h3')].map(node => {
      const style = getComputedStyle(node);
      return {tag: node.tagName, size: parseFloat(style.fontSize), leading: parseFloat(style.lineHeight), height: node.getBoundingClientRect().height};
    }) : [],
  };
}"""


API_CALL = """async ([method, path, body]) => {
  const response = await fetch(path, {method, credentials: 'include', headers: {'Content-Type': 'application/json',
    Authorization: 'Bearer ' + localStorage.getItem('token')}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: response.ok ? await response.json() : null};
}"""


def secret() -> str:
    return Path(BASIC_FILE).read_text().strip()


def new_context(browser: Browser, width: int, height: int, theme: str) -> Any:
    options: dict[str, Any] = {
        "viewport": {"width": width, "height": height},
        "is_mobile": width < 768,
        "has_touch": width < 768,
        "locale": "fr-FR",
        "color_scheme": theme,
    }
    if BASIC_USER and BASIC_FILE:
        options["http_credentials"] = {"username": BASIC_USER, "password": secret()}
    return browser.new_context(**options)


def disposable_token(request: APIRequestContext) -> str:
    """Sign up the disposable first account, or sign in when it already exists."""
    account = {"name": "Phone UI Test", "email": EMAIL, "password": PASSWORD}
    response = request.post(URL + "/api/v1/auths/signup", data=account)
    if response.status != 200:
        response = request.post(
            URL + "/api/v1/auths/signin", data={"email": EMAIL, "password": PASSWORD}
        )
    assert response.status == 200, response.status
    return str(response.json()["token"])


def sign_in(page: Page) -> str:
    """Return a session token; the basic-auth instance signs the browser in on /?connexion."""
    if BASIC_USER and BASIC_FILE:
        page.goto(URL + "/?connexion", wait_until="domcontentloaded")
        page.locator("#chat-input").wait_for(timeout=60_000)
        return str(page.evaluate("() => localStorage.getItem('token')"))
    token = disposable_token(page.context.request)
    page.goto(URL + "/auth", wait_until="domcontentloaded")
    page.evaluate("token => localStorage.setItem('token', token)", token)
    return token


def create_chat(page: Page) -> str:
    """Store a finished user/assistant exchange whose answer holds H1, H2 and H3.

    The call runs in the page (session token and, on the basic-auth instance, its cookie).
    """
    user, assistant, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    messages = {
        user: {
            "id": user,
            "parentId": None,
            "childrenIds": [assistant],
            "role": "user",
            "content": "Fiche compostage",
            "timestamp": now,
        },
        assistant: {
            "id": assistant,
            "parentId": user,
            "childrenIds": [],
            "role": "assistant",
            "content": ANSWER,
            "done": True,
            "timestamp": now,
        },
    }
    chat = {
        "title": "Phone UI check",
        "models": [],
        "history": {"messages": messages, "currentId": assistant},
        "messages": list(messages.values()),
    }
    created = page.evaluate(API_CALL, ["POST", "/api/v1/chats/new", {"chat": chat}])
    assert created["status"] == 200, created["status"]
    return str(created["body"]["id"])


def overlaps(first: dict[str, float], second: dict[str, float]) -> bool:
    return (
        first["left"] < second["right"]
        and second["left"] < first["right"]
        and first["top"] < second["bottom"]
        and second["top"] < first["bottom"]
    )


def check(name: str, data: dict[str, Any]) -> None:
    body = data["body"]
    assert body, f"{name}: no answer paragraph"
    sizes = {head["tag"]: head for head in data["headings"]}
    assert set(sizes) == {"H1", "H2", "H3"}, f"{name}: headings {sorted(sizes)}"
    if name.startswith("phone"):
        toggle = data["toggle"]
        assert toggle, f"{name}: no navbar sidebar toggle"
        box, icon = toggle["box"], toggle["icon"]
        assert box["width"] >= 44 and box["height"] >= 44, f"{name}: toggle {box}"
        assert icon["width"] >= 22 and icon["height"] >= 22, f"{name}: icon {icon}"
        assert toggle["color"] == body["color"], (
            f"{name}: toggle colour {toggle['color']}"
        )
        clash = [c for c in data["controls"] if overlaps(box, c)]
        assert not clash, f"{name}: toggle overlaps {clash}"
        for tag, limit in PHONE_LIMIT.items():
            ratio = sizes[tag]["size"] / body["size"]
            assert ratio <= limit + 0.01, f"{name}: {tag} {ratio:.3f}x body"
            assert sizes[tag]["leading"] <= sizes[tag]["size"] * 1.31, (
                f"{name}: {tag} leading"
            )
        assert sizes["H1"]["height"] <= sizes["H1"]["leading"] * 2 + 1, (
            f"{name}: H1 over 2 lines"
        )
        ordered = [
            sizes["H1"]["size"],
            sizes["H2"]["size"],
            sizes["H3"]["size"],
            body["size"],
        ]
        assert ordered == sorted(ordered, reverse=True) and len(set(ordered)) == 4, (
            f"{name}: hierarchy {ordered}"
        )
    else:
        for tag, scale in DESKTOP_SCALE.items():
            ratio = sizes[tag]["size"] / body["size"]
            assert abs(ratio - scale) < 0.01, f"{name}: {tag} {ratio:.3f}x body"


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    signal.alarm(RUN_TIMEOUT_SECONDS)
    stylesheet = CSS.read_text() if INJECT else ""
    results: dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        setup = new_context(browser, 390, 844, "light")
        page = setup.new_page()
        token = sign_in(page)
        chat = create_chat(page)
        try:
            for name, width, height in VIEWPORTS:
                for theme in ("light", "dark"):
                    context = new_context(browser, width, height, theme)
                    context.add_init_script(
                        f"localStorage.setItem('token', {json.dumps(token)});"
                        " localStorage.setItem('locale', 'fr-FR');"
                        f" localStorage.setItem('theme', {json.dumps(theme)});"
                    )
                    view = context.new_page()
                    view.goto(f"{URL}/c/{chat}", wait_until="domcontentloaded")
                    view.locator(".chat-assistant .markdown-prose h3").wait_for(
                        timeout=60_000
                    )
                    if stylesheet:
                        view.add_style_tag(content=stylesheet)
                    view.keyboard.press("Escape")
                    # Sign-in toasts cover the header for a few seconds; measure the settled page.
                    try:
                        view.wait_for_function(
                            "() => !document.querySelector('[data-sonner-toast]')",
                            timeout=15_000,
                        )
                    except PlaywrightTimeoutError:
                        pass
                    view.wait_for_timeout(1_000)
                    key = f"{name}-{theme}"
                    results[key] = view.evaluate(MEASURE)
                    view.screenshot(path=str(OUTPUT / f"{key}.png"))
                    context.close()
                    check(key, results[key])
        finally:
            (OUTPUT / "measures.json").write_text(json.dumps(results, indent=1))
            page.evaluate(API_CALL, ["DELETE", f"/api/v1/chats/{chat}", None])
            setup.close()
            browser.close()
            signal.alarm(0)
    print(
        "phone UI browser: toggle 44 px and phone headings scaled, desktop scale kept"
    )


if __name__ == "__main__":
    main()
