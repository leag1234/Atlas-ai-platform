"""Privacy level pill and selector on the real pinned chat shell (French, light, dark, phone).

The shell runs the pinned Open WebUI image with the Atlas overlay; a test-only provider
answers. The Atlas privacy routes are the real ones (chat_api app, real store), reached
through the browser's own requests: the session token is verified by the shell itself.
The provider binds each request to its conversation as the Atlas chat adapter does
(conversation_privacy.bind), with the user and conversation ids the shell forwards.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from playwright.sync_api import Page, Route, expect, sync_playwright
import uvicorn

from packages.profiles import PROFILES
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.serving import WEBUI_IMAGE
from services.orchestrator.stream_client import sink_context
from services.orchestrator.webui import configure

OUTPUT = Path("BRAIN/privacy-ui")
PORT = 17961
PROVIDER_PORT = 22981
NAME = "atlas-privacy-ui-17961"
URL = f"http://127.0.0.1:{PORT}"
ANSWER = "Bonjour. Voici une réponse de test, sans aucun appel à un fournisseur."
provider = FastAPI()
bound: list[str] = []


@provider.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": PROFILES[0], "object": "model", "name": "Atlas", "owned_by": "atlas"}
        ],
    }


@provider.post("/v1/chat/completions")
async def completion(request: Request) -> Any:
    from services.orchestrator.conversation_privacy import bind

    body = await request.json()
    # As the Atlas chat adapter: the conversation's level for this request.
    bound.append(
        bind(
            request.headers.get("x-openwebui-user-id", ""),
            request.headers.get("x-openwebui-chat-id", ""),
        )
    )
    if not body.get("stream"):
        return {
            "id": "synthetic-title",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"title":"Essai"}'},
                    "finish_reason": "stop",
                }
            ],
        }
    payload = ChatRequest.model_validate(body)

    async def work(payload: ChatRequest, item: Interaction) -> None:
        sink = sink_context.get()
        assert sink is not None
        await asyncio.sleep(0.2)
        await sink({"delta": {"content": ANSWER}})
        item.reponse, item.state = ANSWER, "done"

    return response(payload, Interaction(), time.monotonic(), work)


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def ready() -> None:
    import urllib.request

    for _ in range(90):
        try:
            with urllib.request.urlopen(URL + "/health", timeout=1) as reply:
                if reply.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError("disposable_ui_not_ready")


@contextmanager
def shell() -> Iterator[None]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for port in (PORT, PROVIDER_PORT):
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(("127.0.0.1", port))
    server = uvicorn.Server(
        uvicorn.Config(
            provider, host="127.0.0.1", port=PROVIDER_PORT, log_level="error"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    environment = {
        "PORT": str(PORT),
        "WEBUI_NAME": "Atlas",
        "ATLAS_STABLE": "1",
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER": "X-Atlas-User-Role",
        "DEFAULT_USER_ROLE": "user",
        "ENABLE_SIGNUP": "False",
        "ENABLE_OLLAMA_API": "False",
        "ENABLE_OPENAI_API": "True",
        "OPENAI_API_BASE_URL": f"http://127.0.0.1:{PROVIDER_PORT}/v1",
        "OPENAI_API_KEY": "synthetic-local-only",
        "ENABLE_FORWARD_USER_INFO_HEADERS": "True",
        "ENABLE_VERSION_UPDATE_CHECK": "False",
        "OFFLINE_MODE": "True",
        "HF_HUB_OFFLINE": "1",
        "DEFAULT_LOCALE": "",
        "ENABLE_MEMORIES": "False",
        "ENABLE_NOTES": "False",
        "ENABLE_PERSISTENT_CONFIG": "False",
        "UI_VISIBILITY": '{"chat.artifacts":false,"chat.code_preview":false}',
        "ENABLE_NOTIFICATIONS": "False",
        "PYTHONPATH": "/app/backend",
    }
    options = [
        "run",
        "-d",
        "--rm",
        "--name",
        NAME,
        "--network",
        "host",
        "--entrypoint",
        "bash",
    ]
    for key, value in environment.items():
        options.extend(["-e", key + "=" + value])
    names = (
        "chat-ui.js",
        "guest-chat.js",
        "chat-ui.css",
        "brand-ui.js",
        "stable-auth.js",
    )
    for source, target in [
        ("packages", "/app/backend/packages"),
        ("prompts/activity-labels.json", "/app/backend/prompts/activity-labels.json"),
        ("assets/brand", "/app/backend/open_webui/static/atlas/brand"),
        (
            "services/orchestrator/static/fonts",
            "/app/backend/open_webui/static/atlas/fonts",
        ),
        (
            "services/orchestrator/static/atlas-version.json",
            "/app/backend/open_webui/static/atlas/atlas-version.json",
        ),
    ] + [
        (
            f"services/orchestrator/{name}",
            "/app/backend/open_webui/static/atlas/" + name,
        )
        for name in names
    ]:
        options.extend(["-v", str(Path(source).resolve()) + ":" + target + ":ro"])
    options.extend([WEBUI_IMAGE, "/app/backend/start.sh"])
    try:
        docker(*options)
        tags = '<link rel="stylesheet" href="/static/atlas/chat-ui.css">' + "".join(
            '<script src="/static/atlas/' + name + '"></script>'
            for name in ("stable-auth.js", "brand-ui.js", "chat-ui.js", "guest-chat.js")
        )
        ready()
        docker(
            "exec",
            NAME,
            "python",
            "-c",
            'from pathlib import Path; p=Path("/app/build/index.html"); p.write_text(p.read_text().replace("</head>", '
            + repr(tags)
            + '+"</head>"))',
        )
        configure(
            URL, {"X-Atlas-User": "bootstrap@localhost", "X-Atlas-User-Role": "admin"}
        )
        yield
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        server.should_exit = True
        thread.join(timeout=10)


def atlas_routes(page: Page, api: TestClient) -> None:
    """Serve /api/atlas/privacy from the real Atlas app, with the browser's own cookie."""

    def route(request: Route) -> None:
        sent = request.request
        path = sent.url.split(URL, 1)[1]
        headers = {
            name: value
            for name, value in sent.headers.items()
            if name in {"cookie", "origin", "sec-fetch-site", "content-type"}
        }
        reply = api.request(
            sent.method, path, content=sent.post_data_buffer, headers=headers
        )
        request.fulfill(
            status=reply.status_code,
            body=reply.content,
            content_type="application/json",
        )

    page.route(URL + "/api/atlas/privacy**", route)


def capture(page: Page, name: str) -> None:
    page.mouse.move(1, 1)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUTPUT / (name + ".png")))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
    print("CAPTURE", name, flush=True)


def pill_clear_of_composer(page: Page) -> None:
    """The pill sits above the composer and covers none of its controls."""
    box = page.locator(".atlas-privacy-pill").bounding_box()
    composer = page.locator("#message-input-container").bounding_box()
    assert box and composer, "pill or composer missing"
    assert box["y"] + box["height"] <= composer["y"], (box, composer)


def pill_clear_of_messages(page: Page) -> None:
    """The pill has its own room below the message list: it never covers a message line."""
    gap = page.evaluate(
        """() => {
      const list = document.getElementById('messages-container');
      const pill = document.querySelector('.atlas-privacy-pill');
      if (!list || !pill) return null;
      return pill.getBoundingClientRect().top - list.getBoundingClientRect().bottom;
    }"""
    )
    assert gap is not None and gap >= 0, gap


def main() -> None:
    with (
        tempfile.TemporaryDirectory() as root,
        patch.dict(
            os.environ,
            {
                "ATLAS_CONVERSATION_PRIVACY_DB": str(Path(root) / "levels.sqlite"),
                "ATLAS_WEBUI_URL": URL,
                "ATLAS_PROJECT_ORIGIN": URL,
                "ATLAS_PROJECT_MODE": "0",
                "ATLAS_PROJECT_DSN": "",
                "ATLAS_PRIVACY_ENHANCED": "1",
                "ATLAS_PRIVACY_MINIMUM": "",
                # Only its presence is read (Strict can be chosen); nothing is called.
                "ATLAS_OWNED_VLLM_BASE_URL": "http://127.0.0.1:9/v1",
                "ATLAS_INTERACTION_DIR": str(Path(root) / "turns"),
            },
        ),
        shell(),
        sync_playwright() as pw,
    ):
        from services.orchestrator.chat_api import app

        api = TestClient(app, base_url=URL)
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        results: dict[str, Any] = {}
        for mode, width, height in [
            ("light", 1440, 1000),
            ("dark", 1440, 1000),
            ("phone", 390, 844),
        ]:
            context = browser.new_context(
                locale="fr-FR",
                viewport={"width": width, "height": height},
                has_touch=mode == "phone",
                is_mobile=mode == "phone",
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
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            atlas_routes(page, api)
            page.goto(URL)
            try:
                page.locator("#chat-input").wait_for(timeout=120000)
            except Exception:
                page.screenshot(path=str(OUTPUT / (mode + "-load-failure.png")))
                print("LOAD FAILURE", page.url, errors, flush=True)
                raise
            pill = page.locator(".atlas-privacy-pill")
            expect(pill).to_be_visible(timeout=20000)
            expect(pill).to_have_text("Standard")
            pill_clear_of_composer(page)
            capture(page, mode + "-new")
            pill.click()
            menu = page.locator(".atlas-privacy-menu")
            expect(menu).to_be_visible()
            expect(menu.locator(".atlas-privacy-choice")).to_have_count(3)
            # An owned server is configured: Enhanced runs there (nothing leaves Atlas).
            expect(menu.locator('[data-level="enhanced"]')).to_be_enabled()
            expect(menu.locator('[aria-checked="true"]')).to_have_attribute(
                "data-level", "standard"
            )
            capture(page, mode + "-menu")
            menu.locator('[data-level="strict"]').click()
            expect(menu).to_be_hidden()
            expect(pill).to_have_text("Stricte")
            expect(pill).to_have_attribute("data-level", "strict")
            capture(page, mode + "-strict")
            before = len(bound)
            page.locator("#chat-input").click()
            page.keyboard.type("Bonjour")
            # On a phone, Enter adds a line: send with the button everywhere.
            page.locator("#send-message-button").click()
            try:
                page.get_by_text(ANSWER).first.wait_for(timeout=60000)
            except Exception:
                page.screenshot(path=str(OUTPUT / (mode + "-failure.png")))
                print("FAILURE", page.url, bound, flush=True)
                raise
            page.wait_for_url("**/c/**", timeout=30000)
            assert bound[before] == "strict", bound
            expect(pill).to_have_text("Stricte", timeout=20000)
            pill_clear_of_composer(page)
            pill_clear_of_messages(page)
            capture(page, mode + "-answer")
            pill.click()
            standard = menu.locator('[data-level="standard"]')
            expect(standard).to_be_disabled()
            expect(standard).to_contain_text("contient déjà des messages")
            capture(page, mode + "-started")
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
            assert not errors, errors
            results[mode] = {"bound": bound[before:], "url": page.url.split("/c/")[0]}
            context.close()
        browser.close()
        print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
