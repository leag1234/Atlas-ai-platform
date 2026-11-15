"""Cold phone loads behind the basic-auth front door: the trusted-header sign-in reaches the composer.

Two phones, each a fresh context (cold cache, no cookie):
- "credentials": the browser answers every basic-auth challenge (Playwright http_credentials);
- "no subresource credentials": the browser sends the basic credentials only with the page and
  its fetch/XHR/WebSocket calls, never with scripts, styles, images, fonts or the manifest, as
  Chrome on Android did on 2026-11-15 (about 200 module scripts answered 401 and never retried).
Both must sign in through the trusted header and show the composer, and no interface file may be
refused. The front door must still refuse an anonymous page or API read.

Environment: ATLAS_STABLE_BASE_URL, ATLAS_STABLE_BASIC_USER, ATLAS_STABLE_BASIC_PASSWORD (as
stable_front_door_browser.py, which runs these checks first). Output never contains credentials.
"""

import base64
import json
import os
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Playwright, Request, sync_playwright

BASE_URL = os.environ.get("ATLAS_STABLE_BASE_URL", "http://127.0.0.1:8900/")
PHONE = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
    "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
    "locale": "fr-FR",
}
# Calls the page makes itself; everything else is a subresource the browser loads.
PAGE_CALLS = ("document", "fetch", "xhr", "websocket", "eventsource")


def loopback() -> bool:
    return urlparse(BASE_URL).hostname in ("127.0.0.1", "localhost", "::1")


def basic() -> tuple[str, str]:
    return os.environ["ATLAS_STABLE_BASIC_USER"], os.environ[
        "ATLAS_STABLE_BASIC_PASSWORD"
    ]


def cold_load(playwright: Playwright, subresource_credentials: bool) -> dict[str, Any]:
    user, password = basic()
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    options: dict[str, Any] = dict(PHONE, ignore_https_errors=loopback())
    if subresource_credentials:
        options["http_credentials"] = {"username": user, "password": password}
    context = browser.new_context(**options)
    if not subresource_credentials:
        token = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

        def route(route_request: Any) -> None:
            request = route_request.request
            if request.resource_type in PAGE_CALLS:
                route_request.continue_(
                    headers={**request.headers, "authorization": token}
                )
            else:
                route_request.continue_()

        context.route("**/*", route)
    page = context.new_page()
    refused: list[str] = []
    signin: list[int] = []
    user_pictures: list[str] = []

    def on_response(response: Any) -> None:
        request: Request = response.request
        path = urlparse(response.url).path
        if path == "/api/v1/auths/signin":
            signin.append(response.status)
        if response.status >= 400 and request.resource_type not in PAGE_CALLS:
            item = f"{response.status} {request.resource_type} {path[:80]}"
            # Pictures of a user or a model (/api/..., /images/...) are user data, not
            # interface files: they keep needing the user, so this model of a browser that
            # never sends credentials with a subresource cannot load them.
            if request.resource_type == "image" and path.startswith(
                ("/api/", "/images/")
            ):
                user_pictures.append(item)
            else:
                refused.append(item)

    def on_failed(request: Request) -> None:
        if request.resource_type not in PAGE_CALLS:
            refused.append(
                f"{request.failure} {request.resource_type} {urlparse(request.url).path[:80]}"
            )

    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    started = time.monotonic()
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("#chat-input", state="visible", timeout=40000)
    seconds = round(time.monotonic() - started, 1)
    browser.close()
    assert signin and signin[0] == 200, f"trusted-header sign-in: {signin}"
    assert not refused, f"interface files refused: {refused[:8]}"
    assert subresource_credentials is False or not user_pictures, user_pictures
    return {
        "composer_s": seconds,
        "signin": signin[0],
        "user_pictures_refused": len(user_pictures),
    }


def anonymous_edge(playwright: Playwright) -> dict[str, int]:
    """Interface files are public and session-free; pages and APIs still need basic credentials."""
    anonymous = playwright.request.new_context(ignore_https_errors=loopback())
    statuses = {}
    try:
        for path in (
            "",
            "api/config",
            "api/v1/auths/",
            "static/loader.js",
            "_app/version.json",
        ):
            response = anonymous.get(urljoin(BASE_URL, path), max_redirects=0)
            statuses["/" + path] = response.status
            if path.startswith(("static/", "_app/")):
                assert "set-cookie" not in response.headers, path
        post = anonymous.post(urljoin(BASE_URL, "static/loader.js"), data="x")
        statuses["POST /static/loader.js"] = post.status
    finally:
        anonymous.dispose()
    assert statuses["/api/config"] == 401 and statuses["/api/v1/auths/"] == 401, (
        statuses
    )
    assert (
        statuses["/static/loader.js"] == 200 and statuses["/_app/version.json"] == 200
    ), statuses
    assert statuses["POST /static/loader.js"] in (401, 403, 405), statuses
    return statuses


def check(playwright: Playwright, loads: int = 2) -> dict[str, Any]:
    result: dict[str, Any] = {"anonymous_edge": anonymous_edge(playwright)}
    for name, credentials in (
        ("credentials", True),
        ("no_subresource_credentials", False),
    ):
        result[name] = [cold_load(playwright, credentials) for _ in range(loads)]
    return result


def main() -> None:
    with sync_playwright() as playwright:
        print(
            json.dumps(
                check(playwright, int(os.environ.get("ATLAS_PHONE_LOADS", "3")))
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
