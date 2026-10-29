"""Real pinned guest shell with a test-only provider and restart fault injection."""

import asyncio
from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI, Request
from playwright.sync_api import FilePayload, Page, sync_playwright, expect
import uvicorn

from packages.profiles import PROFILES
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.webui import configure

OUTPUT = Path("BRAIN/chat-ui-polish")
PORT = 17940
PROVIDER_PORT = 22960
NAME = "atlas-chat-ui-polish-17940"
URL = f"http://127.0.0.1:{PORT}"
ANSWER = (
    "## Le cycle de l\u2019eau\n\n"
    "Le soleil chauffe les mers, les lacs et les rivi\u00e8res. Une partie de l\u2019eau "
    "s\u2019\u00e9vapore et monte dans l\u2019air.\n\n"
    "### Des nuages \u00e0 la pluie\n\n"
    "En altitude, l\u2019air se refroidit. La vapeur se condense en petites gouttes "
    "qui forment les nuages. Quand elles deviennent assez lourdes, elles tombent "
    "sous forme de pluie ou de neige.\n\n"
    "### Le retour vers les rivi\u00e8res\n\n"
    "Une partie de l\u2019eau s\u2019infiltre dans le sol et alimente les nappes "
    "souterraines. Le reste ruisselle vers les rivi\u00e8res, puis rejoint la mer.\n\n"
    "1. Le soleil apporte l\u2019\u00e9nergie.\n"
    "2. L\u2019eau change d\u2019\u00e9tat.\n"
    "3. Les pr\u00e9cipitations redistribuent l\u2019eau.\n\n"
    "**\u00c0 retenir :** ce cycle relie l\u2019atmosph\u00e8re, les sols et les oc\u00e9ans."
)
requests: list[dict[str, Any]] = []
waited: set[str] = set()
app = FastAPI()


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": PROFILES[0], "object": "model", "name": "Atlas", "owned_by": "atlas"}
        ],
    }


@app.post("/v1/chat/completions")
async def completion(request: Request) -> Any:
    body = await request.json()
    print("PROVIDER", "stream" if body.get("stream") else "task", flush=True)
    requests.append(body)
    payload = ChatRequest.model_validate(body)
    if not body.get("stream"):
        return {
            "id": "synthetic-title",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"title":"le cycle de l\u2019eau dans notre environnement quotidien"}',
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    async def work(payload: ChatRequest, item: Interaction) -> None:
        sink = sink_context.get()
        assert sink is not None
        if "attendre" in payload.user_text.lower() and payload.user_text not in waited:
            waited.add(payload.user_text)
            await asyncio.sleep(65)
        else:
            await asyncio.sleep(0.2)
        for start in range(0, len(ANSWER), 70):
            await sink({"delta": {"content": ANSWER[start : start + 70]}})
            await asyncio.sleep(0.02)
        item.reponse, item.state = ANSWER, "done"

    return response(payload, Interaction(), time.monotonic(), work)


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


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
def guest_shell() -> Iterator[None]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for port in (PORT, PROVIDER_PORT):
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(("127.0.0.1", port))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PROVIDER_PORT, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
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
        "ENABLE_VERSION_UPDATE_CHECK": "False",
        "OFFLINE_MODE": "True",
        "HF_HUB_OFFLINE": "1",
        "DEFAULT_LOCALE": "",
        "ENABLE_MEMORIES": "False",
        "ENABLE_AUTOMATIONS": "False",
        "ENABLE_NOTES": "False",
        "ENABLE_EASTER_EGGS": "False",
        "ENABLE_CALENDAR": "False",
        "ENABLE_PERSISTENT_CONFIG": "False",
        "PYTHONPATH": "/app/backend",
    }
    for key, value in environment.items():
        options.extend(["-e", key + "=" + value])
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
        for name in (
            "chat-ui.js",
            "guest-chat.js",
            "chat-ui.css",
            "brand-ui.js",
            "stable-auth.js",
            "settings-ui.js",
            "settings-locales.json",
        )
    ]:
        options.extend(["-v", str(Path(source).resolve()) + ":" + target + ":ro"])
    options.extend(["ghcr.io/open-webui/open-webui:v0.11.3", "/app/backend/start.sh"])
    with patch.dict(
        os.environ,
        {
            "ATLAS_STABLE": "1",
            "ATLAS_INTERACTION_DIR": str(OUTPUT / "synthetic-turn-logs"),
        },
    ):
        try:
            docker(*options)
            tags = (
                '<link rel="stylesheet" href="/static/atlas/chat-ui.css">'
                + "".join(
                    '<script src="/static/atlas/' + name + '"></script>'
                    for name in (
                        "stable-auth.js",
                        "brand-ui.js",
                        "chat-ui.js",
                        "guest-chat.js",
                    )
                )
                + '<script type="module" src="/static/atlas/settings-ui.js"></script>'
            )
            docker(
                "exec",
                NAME,
                "python",
                "-c",
                'from pathlib import Path; p=Path("/app/build/index.html"); p.write_text(p.read_text().replace("</head>", '
                + repr(tags)
                + '+"</head>"))',
            )
            ready()
            print("SERVER READY", flush=True)
            configure(
                URL,
                {"X-Atlas-User": "bootstrap@localhost", "X-Atlas-User-Role": "admin"},
            )
            print("CONFIGURED", flush=True)
            yield
        finally:
            subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
            server.should_exit = True
            thread.join(timeout=10)


def snapshot(page: Page, name: str) -> None:
    if name.endswith("-interrupted"):
        page.evaluate(
            "document.querySelectorAll('[data-sonner-toast] [data-close-button]').forEach(button => button.click())"
        )
        expect(page.locator("[data-sonner-toast]")).to_have_count(0)
    page.mouse.move(1, 1)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUTPUT / (name + ".png")))
    print("CAPTURE", name, flush=True)
    (OUTPUT / (name + ".html")).write_text(page.content())
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
    assert " / 120s" not in page.locator("body").inner_text(), name
    assert "Thinking" not in page.locator("body").inner_text(), name
    assert (
        page.locator("[data-atlas-footer]").inner_text()
        == "Atlas "
        + json.loads(
            Path("services/orchestrator/static/atlas-version.json").read_text()
        )["version"]
    ), name


def main() -> None:
    with guest_shell(), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for mode, width, height in [
            ("light", 1440, 1000),
            ("dark", 1440, 1000),
            ("phone", 390, 844),
        ]:
            if mode not in os.environ.get(
                "ATLAS_POLISH_MODES", "light,dark,phone"
            ).split(","):
                continue
            context = browser.new_context(
                locale="fr-FR",
                viewport={"width": width, "height": height},
                permissions=["clipboard-read", "clipboard-write"],
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
            page.on(
                "console",
                lambda message: print("BROWSER ERROR", message.text[:400], flush=True)
                if message.type == "error"
                and not any(word in message.text for word in ("Bearer", "eyJ", "token"))
                else None,
            )
            page.goto(URL)
            page.wait_for_timeout(6000)
            page.screenshot(path=str(OUTPUT / "loading.png"))
            print(
                "LOADING",
                mode,
                page.url,
                page.locator("body").inner_text()[-700:],
                errors,
                flush=True,
            )
            page.locator("#chat-input").wait_for(timeout=60000)
            page.wait_for_timeout(5000)
            snapshot(page, mode + "-home")
            assert (
                page.locator("#chat-input p").evaluate(
                    "e=>getComputedStyle(e, '::before').content"
                )
                == '"Envoyer un message"'
            )
            page.locator("#input-menu-button").click()
            page.locator('input[type="file"]:not(#camera-input)').set_input_files(
                [
                    FilePayload(
                        name="rapport_technique_complet_pour_la_relecture.txt",
                        mimeType="text/plain",
                        buffer=b"A" * 971878,
                    ),
                    FilePayload(
                        name="mesures.csv",
                        mimeType="text/csv",
                        buffer=b"jour,mesure\n1,4\n",
                    ),
                ]
            )
            page.keyboard.press("Escape")
            chips = page.locator("#message-input-container .atlas-file-chip")
            expect(chips).to_have_count(2, timeout=30000)
            expect(chips.first.locator(".atlas-file-size")).to_have_text("949 Ko")
            expect(chips.first.locator(".atlas-file-name")).to_have_attribute(
                "title", "rapport_technique_complet_pour_la_relecture.txt"
            )
            expect(
                chips.last.locator('svg[data-atlas-file-type="table"]')
            ).to_be_visible()
            assert (
                page.locator("#chat-input p").evaluate(
                    "e=>getComputedStyle(e, '::before').content"
                )
                == '"Envoyer un message"'
            )
            assert chips.evaluate_all("""chips => chips.every(chip => {
                const size = chip.querySelector('.atlas-file-size').getBoundingClientRect();
                const remove = chip.querySelector('button[aria-label]').getBoundingClientRect();
                return size.right <= remove.left + 1 && size.width > 0;
            })""")
            snapshot(page, mode + "-attached")
            page.locator(
                '#message-input-container button[aria-label="Retirer le fichier"]'
            ).first.click()
            page.locator(
                '#message-input-container button[aria-label="Retirer le fichier"]'
            ).first.click()
            page.locator("#chat-input").fill(
                "Bonjour, explique le cycle de l\u2019eau."
            )
            page.locator("#send-message-button").click()
            expect(page.locator(".chat-assistant")).to_contain_text(
                "Le cycle", timeout=60000
            )
            page.locator(".regenerate-response-button").wait_for(state="attached")
            page.wait_for_timeout(1000)
            snapshot(page, mode + "-long-answer")
            row = page.locator(".atlas-actions").last
            assert row.locator("[data-atlas-more]").count() == 1
            assert row.locator(".atlas-overflow-action").count() >= 2
            assert row.evaluate("e=>getComputedStyle(e).opacity") == (
                "1" if mode == "phone" else "0"
            )
            visible_actions = row.locator("button").evaluate_all("""buttons => buttons
                .filter(button => button.getBoundingClientRect().width > 0)
                .map(button => button.dataset.atlasAction || 'more').sort()""")
            assert visible_actions == ["1", "2", "3", "4", "6", "more"]
            row.locator(".copy-response-button").click()
            assert "Le cycle" in page.evaluate("navigator.clipboard.readText()")
            row.locator("[data-atlas-more]").click()
            expect(page.locator(".atlas-action-menu")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator(".atlas-action-menu")).to_have_count(0)
            title = page.locator("nav .truncate.text-left").first
            shown, full = title.inner_text(), title.get_attribute("title")
            assert shown[0].isupper(), shown
            assert full is not None
            assert shown == full or (
                shown.endswith("\u2026")
                and full.startswith(shown[:-1])
                and full[len(shown) - 1].isspace()
            )
            assert title.evaluate("e=>e.scrollWidth <= e.clientWidth")
            bubble = page.locator(".user-message").last
            assert bubble.evaluate("e=>getComputedStyle(e).backgroundColor") == (
                "rgb(48, 48, 46)" if mode == "dark" else "rgb(241, 240, 237)"
            )
            if mode == "phone":
                page.goto(URL)
                page.locator("#chat-input").wait_for()
            question = (
                "Bonjour, attendre avant de poursuivre : "
                + {
                    "light": "premier exemple",
                    "dark": "second exemple",
                    "phone": "dernier exemple",
                }[mode]
            )
            page.locator("#chat-input").fill(question)
            page.locator("#send-message-button").click()
            expect(page.locator("#stop-response-button")).to_be_visible()
            page.wait_for_timeout(12000)
            expect(page.locator(".chat-assistant").last).to_contain_text(
                "R\u00e9flexion"
            )
            snapshot(page, mode + "-progress")
            docker("restart", NAME)
            expect(page.locator(".atlas-interruption")).to_contain_text(
                "La r\u00e9ponse a \u00e9t\u00e9 interrompue.", timeout=30000
            )
            ready()
            page.wait_for_timeout(3000)
            snapshot(page, mode + "-interrupted")
            count = len(requests)
            page.get_by_role("button", name="R\u00e9essayer", exact=True).click()
            expect(page.locator(".chat-assistant").last).to_contain_text(
                "Le cycle", timeout=30000
            )
            assert len(requests) > count
            resent = [item for item in requests[count:] if item.get("stream")]
            assert resent[-1]["messages"][-1]["content"] == question
            assert not page.locator(".atlas-interruption").count()
            assert not errors, errors
            print(
                "PASS",
                mode,
                "home, attachment, long answer, progress, restart, retry",
                flush=True,
            )
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
