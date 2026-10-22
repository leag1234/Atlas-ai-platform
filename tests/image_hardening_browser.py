"""Real local UI and SSE pipeline against a scripted HTTP image gateway; no GPU."""

import base64
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
from threading import Thread
import time
from typing import Any
from unittest.mock import patch
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw
from playwright.sync_api import Page, expect, sync_playwright
import uvicorn

from packages.ports import atlas_ports
from services.orchestrator.chat_api import app
from services.orchestrator.terminal_stack import adapter_socket, network_gateway

OUTPUT = Path("BRAIN/image-hardening")


def fixture_image() -> str:
    canvas = Image.new("RGB", (1024, 1024), "#eef2ed")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((100, 780, 924, 850), radius=25, fill="#344b45")
    draw.rectangle((210, 460, 430, 780), fill="#849d9a", outline="#344b45", width=10)
    draw.ellipse((210, 410, 430, 510), fill="#bdcdca", outline="#344b45", width=10)
    draw.ellipse((520, 370, 890, 740), fill="#b47843", outline="#344b45", width=18)
    draw.ellipse((575, 425, 835, 685), fill="#eef2ed", outline="#344b45", width=8)
    draw.line((320, 510, 705, 555), fill="#344b45", width=24)
    draw.line((705, 555, 705, 780), fill="#344b45", width=20)
    draw.ellipse((680, 530, 730, 580), fill="#b47843")
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class ScriptedGateway(BaseHTTPRequestHandler):
    fail = False

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        status = 200
        result: dict[str, object]
        if self.path == "/images/ready":
            result = {"ready": False}
        elif self.path == "/images/start":
            time.sleep(3)
            result = {"ready": True}
        elif self.path == "/images/generate":
            time.sleep(6)
            if self.fail:
                status, result = (
                    429,
                    {
                        "code": "quota_exceeded",
                        "measured": 11,
                        "limit": 10,
                        "unit": "requests",
                    },
                )
            else:
                result = {
                    "image": fixture_image(),
                    "cost_eur": 0,
                    "width": 1024,
                    "height": 1024,
                    "seed": 7,
                }
        else:
            status, result = 503, {"code": "provider_error"}
        data = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def ui_api(base: str, path: str, token: str = "", body: object = None) -> Any:
    request = Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def submit(page: Page, base: str, question: str) -> None:
    page.goto(base, wait_until="networkidle")
    welcome = page.get_by_text("Okay, Let's Go!", exact=True)
    if welcome.count():
        welcome.click()
    page.locator("#chat-input").fill(question)
    page.locator("#chat-input").press("Enter")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    ports = atlas_ports()
    base = ports.url("chat_ui")
    token = ui_api(
        base, "/api/v1/auths/signin", body={"email": "admin@localhost", "password": ""}
    )["token"]
    configuration = ui_api(base, "/openai/config", token)
    listener = adapter_socket(ports.port("adapter") + 3)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedGateway)
    gateway_thread = Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    worker = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "ATLAS_GATEWAY_URL": f"http://127.0.0.1:{gateway.server_port}",
                    "ATLAS_IMAGE_ON_DEMAND": "1",
                },
            )
        )
        stack.enter_context(
            patch("services.orchestrator.chat_stream.write_interaction")
        )
        stack.enter_context(patch("services.orchestrator.chat_api.write_interaction"))
        worker.start()
        try:
            until = time.monotonic() + 10
            while not server.started and time.monotonic() < until:
                time.sleep(0.05)
            assert server.started
            test_config = {
                **configuration,
                "OPENAI_API_BASE_URLS": [
                    f"http://{network_gateway()}:{ports.port('adapter') + 3}/v1"
                ],
                "OPENAI_API_KEYS": ["atlas-local"],
                "OPENAI_API_CONFIGS": {},
            }
            ui_api(base, "/openai/config/update", token, test_config)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, args=["--no-sandbox"]
                )
                page = browser.new_page(viewport={"width": 1500, "height": 1100})
                page.add_init_script("localStorage.setItem('atlas-ui-language', 'fr');")
                submit(page, base, "génère une image de moteur Stirling")
                card = page.locator(".atlas-image-waiting")
                try:
                    expect(card).to_be_visible(timeout=30000)
                except AssertionError:
                    (OUTPUT / "browser-dom.html").write_text(
                        page.locator(".chat-assistant").last.evaluate(
                            "node => node.outerHTML"
                        )
                    )
                    page.screenshot(path=str(OUTPUT / "browser-debug.png"))
                    raise
                expect(card).to_contain_text("Démarrage du GPU", timeout=10000)
                card.scroll_into_view_if_needed()
                page.screenshot(path=str(OUTPUT / "placeholder-starting.png"))
                expect(card).to_contain_text("Génération en cours", timeout=10000)
                (OUTPUT / "browser-dom.html").write_text(
                    page.locator(".chat-assistant").last.evaluate(
                        "node => node.outerHTML"
                    )
                )
                waiting = card.bounding_box()
                assert waiting and abs(waiting["width"] - waiting["height"]) < 2
                assert (
                    card.locator(".atlas-image-spinner").evaluate(
                        "node => getComputedStyle(node).animationName"
                    )
                    == "atlas-image-spin"
                )
                page.screenshot(path=str(OUTPUT / "placeholder.png"))
                image = page.locator(
                    '.chat-assistant .markdown-prose img[src*="/images/"]'
                )
                expect(image).to_be_visible(timeout=30000)
                expect(card).to_have_count(0)
                assert image.evaluate(
                    "img => img.complete && img.naturalWidth === 1024"
                )
                final = image.bounding_box()
                assert final and abs(final["width"] - waiting["width"]) < 2
                image.scroll_into_view_if_needed()
                page.screenshot(path=str(OUTPUT / "image.png"))
                ScriptedGateway.fail = True
                submit(page, base, "génère une image de cube")
                failed = page.locator('.atlas-image-waiting[data-state="failed"]')
                expect(failed).to_be_visible(timeout=30000)
                expect(failed).to_contain_text("Quota d’images épuisé")
                expect(failed).to_contain_text("11 requests")
                expect(failed).to_contain_text("10 requests")
                expect(failed.locator(".atlas-image-spinner")).to_be_hidden()
                failed.scroll_into_view_if_needed()
                page.screenshot(path=str(OUTPUT / "failure.png"))
                (OUTPUT / "browser.json").write_text(
                    json.dumps(
                        {
                            "mode": "scripted-image-gateway",
                            "placeholder": waiting,
                            "image": final,
                            "steps": ["starting", "generating", "failed"],
                            "live_calls": 0,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                browser.close()
        finally:
            ui_api(base, "/openai/config/update", token, configuration)
            server.should_exit = True
            worker.join(timeout=10)
            listener.close()
            gateway.shutdown()
            gateway.server_close()
            gateway_thread.join(timeout=10)
    print(
        "Image waiting, delivery and failure verified on the local page; scripted gateway, zero live calls"
    )


if __name__ == "__main__":
    main()
