"""Full stable acceptance against an environment-selected URL and two guests.

Export ATLAS_PORT_BASE=1900 before running. Required: ATLAS_STABLE_BASE_URL,
ATLAS_STABLE_BASIC_USER/PASSWORD and ATLAS_STABLE_BOB_USER/PASSWORD.
Use --maintenance only after stopping the disposable upstream. No production
service is stopped by this harness. Output contains checks, never session tokens.
"""

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urljoin, urlparse
from zipfile import ZipFile

from reportlab.pdfgen.canvas import Canvas
from PIL import Image
from playwright.sync_api import (
    BrowserContext,
    FilePayload,
    HttpCredentials,
    Page,
    sync_playwright,
)


BASE_URL = os.environ.get("ATLAS_STABLE_BASE_URL", "http://127.0.0.1:8900/")
OUTPUT = Path(os.environ.get("ATLAS_STABLE_REPORT_DIR", "reports/stable-integration"))


def credentials(bob: bool = False) -> HttpCredentials:
    prefix = "ATLAS_STABLE_BOB" if bob else "ATLAS_STABLE_BASIC"
    return {
        "username": os.environ[prefix + "_USER"],
        "password": os.environ[prefix + "_PASSWORD"],
    }


def auth_header(bob: bool = False) -> dict[str, str]:
    auth = credentials(bob)
    token = base64.b64encode(f"{auth['username']}:{auth['password']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def assert_same_origin(page: Page) -> None:
    expected = urlparse(BASE_URL).netloc
    requests = page.evaluate(
        "performance.getEntriesByType('resource').map(e => e.name)"
    )
    assert all(urlparse(url).netloc in ("", expected) for url in requests)


def api(page: Page, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """async ({path, body}) => {
        const response = await fetch(path, {
            method: body === null ? 'GET' : 'POST',
            headers: {'Content-Type': 'application/json',
                      Authorization: `Bearer ${localStorage.token}`},
            ...(body === null ? {} : {body: JSON.stringify(body)})
        });
        return {status: response.status, body: await response.json()};
    }""",
            {"path": path, "body": body},
        )
    )


def signin(context: BrowserContext, bob: bool = False) -> dict[str, Any]:
    response = context.request.post(
        urljoin(BASE_URL, "api/v1/auths/signin"),
        headers=auth_header(bob),
        data={"email": "ignored", "password": "ignored"},
    )
    assert response.status == 200, f"signin status {response.status}"
    session = dict(response.json())
    assert session["role"] == "user"
    assert session["email"].split("@", 1)[0] == credentials(bob)["username"]
    return session


def open_page(context: BrowserContext, session: dict[str, Any]) -> Page:
    page = context.new_page()
    console_errors: list[str] = []
    missing: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on(
        "response",
        lambda response: missing.append(response.url)
        if response.status == 404
        else None,
    )
    response = page.goto(BASE_URL, wait_until="domcontentloaded")
    assert response and response.ok
    assert "noindex" in response.headers.get("x-robots-tag", "")
    page.evaluate("token => localStorage.setItem('token', token)", session["token"])
    page.reload(wait_until="networkidle")
    page.locator("#chat-input").wait_for(timeout=60_000)
    assert page.locator("#atlas-ui-toggle").count() == 1
    page.locator("#atlas-ui-toggle").click()
    assert page.locator("#atlas-ui-popover").is_visible()
    page.keyboard.press("Escape")
    assert_same_origin(page)
    assert not console_errors, console_errors
    assert not missing, missing
    return page


def websocket_ping(page: Page) -> None:
    result = page.evaluate("""() => new Promise((resolve, reject) => {
        const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
        const socket = new WebSocket(`${scheme}://${location.host}/ws/socket.io/?EIO=4&transport=websocket`);
        const timer = setTimeout(() => {socket.close(); reject(new Error('websocket timeout'));}, 10000);
        socket.onmessage = event => {
            if (event.data.startsWith('0')) socket.send('40');
            if (event.data.startsWith('40')) {
                clearTimeout(timer); socket.close(); resolve('namespace-connected');
            }
        };
        socket.onerror = () => {clearTimeout(timer); reject(new Error('websocket error'));};
    })""")
    assert result == "namespace-connected"


def upload_files(page: Page) -> str:
    image_buffer = BytesIO()
    Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3)).save(
        image_buffer, "PNG"
    )
    image_bytes = image_buffer.getvalue()
    assert len(image_bytes) > 65_536
    pdf_buffer = BytesIO()
    document = Canvas(pdf_buffer)
    document.drawString(72, 720, "Synthetic acceptance document: a blue square.")
    document.save()
    pdf_bytes = pdf_buffer.getvalue()
    page.locator("#input-menu-button").click()
    chooser = page.locator('input[type="file"]:not(#camera-input)')
    chooser.wait_for(state="attached")
    with page.expect_response(
        lambda response: "/api/v1/files/" in response.url
        and response.request.method == "POST",
        timeout=30_000,
    ) as uploaded:
        chooser.set_input_files(
            [
                FilePayload(
                    name="synthetic-atlas-upload.png",
                    mimeType="image/png",
                    buffer=image_bytes,
                ),
                FilePayload(
                    name="synthetic-atlas-upload.pdf",
                    mimeType="application/pdf",
                    buffer=pdf_bytes,
                ),
            ]
        )
    response = uploaded.value
    assert response.ok, f"upload status {response.status}"
    file_id = str(response.json()["id"])
    page.locator('#message-input-container img[data-cy="image"]').wait_for()
    page.get_by_text("synthetic-atlas-upload.pdf", exact=False).first.wait_for()
    # Verify originals traversed nginx and the relay, not just a local preview.
    for name, mime, data in (
        ("synthetic-atlas-upload.png", "image/png", image_bytes),
        ("synthetic-atlas-upload.pdf", "application/pdf", pdf_bytes),
    ):
        uploaded_original = page.context.request.post(
            urljoin(BASE_URL, "api/v1/files/"),
            headers=auth_header(),
            multipart={"file": {"name": name, "mimeType": mime, "buffer": data}},
        )
        assert uploaded_original.ok
        original_id = uploaded_original.json()["id"]
        original = page.context.request.get(
            urljoin(BASE_URL, "api/v1/files/" + original_id + "/content"),
            headers=auth_header(),
        )
        assert original.ok and original.body() == data
    assert_same_origin(page)
    return file_id


def stream(page: Page, model: str, question: str) -> str:
    result = page.evaluate(
        """async ({model, question}) => {
        const response = await fetch('/api/chat/completions', {
            method: 'POST', headers: {'Content-Type': 'application/json',
                Authorization: `Bearer ${localStorage.token}`},
            body: JSON.stringify({model, stream: true,
                messages: [{role: 'user', content: question}]})
        });
        const reader = response.body.getReader();
        const decoder = new TextDecoder(); let text = ''; let reads = 0;
        while (true) {const {done, value} = await reader.read();
            if (done) break; reads++; text += decoder.decode(value, {stream: true});}
        return {status: response.status, contentType: response.headers.get('content-type'), text, reads};
    }""",
        {"model": model, "question": question},
    )
    assert result["status"] == 200, f"stream status {result['status']}"
    assert "text/event-stream" in result["contentType"]
    assert result["reads"] >= 2, "stream was buffered"
    answer = ""
    for line in result["text"].splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            event = json.loads(line[5:])
            for choice in event.get("choices", []):
                answer += choice.get("delta", {}).get("content", "")
    assert answer.strip(), "empty streamed answer"
    return answer


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(http_credentials=credentials())
        context.set_extra_http_headers(auth_header())
        if "--maintenance" in sys.argv:
            response = context.request.get(BASE_URL)
            assert response.status in (502, 504)
            assert "maintenance" in response.text().lower()
            assert "noindex" in response.headers.get("x-robots-tag", "")
            (OUTPUT / "maintenance.json").write_text(
                json.dumps({"status": response.status, "maintenance": True})
            )
            browser.close()
            return
        anonymous = playwright.request.new_context()
        assert anonymous.get(BASE_URL).status == 401
        anonymous.dispose()
        alice = signin(context)
        page = open_page(context, alice)
        websocket_ping(page)
        forged = context.request.post(
            urljoin(BASE_URL, "api/v1/auths/signin"),
            headers={
                **auth_header(),
                "X-Atlas-User": "intruder@example.invalid",
                "X-Atlas-Proxy-Secret": "forged",
                "X-Atlas-User-Role": "admin",
                "X-OpenWebUI-User-Role": "admin",
                "X-Forwarded-Role": "admin",
            },
            data={"email": "ignored", "password": "ignored"},
        )
        assert forged.status == 200
        assert forged.json()["id"] == alice["id"] and forged.json()["role"] == "user"
        signup = context.request.post(
            urljoin(BASE_URL, "api/v1/auths/signup"),
            headers=auth_header(),
            data={
                "name": "intruder",
                "email": "intruder@example.invalid",
                "password": "synthetic-test-password",
            },
        )
        assert signup.status == 403
        terminal_denied = context.request.post(
            urljoin(BASE_URL, "api/v1/terminals/atlas-files/execute"),
            headers=auth_header(),
            data={"command": "true"},
        )
        assert terminal_denied.status == 403
        models = api(page, "/api/models")
        assert models["status"] == 200
        model = str(models["body"]["data"][0]["id"])
        answer = stream(page, model, "Translate hello into French.")
        assert "bonjour" in answer.lower()
        saved = api(
            page,
            "/api/v1/chats/new",
            {
                "chat": {
                    "title": "Alice private acceptance",
                    "models": [model],
                    "messages": [
                        {
                            "id": "u1",
                            "role": "user",
                            "content": "Translate hello into French.",
                        },
                        {"id": "a1", "role": "assistant", "content": answer},
                    ],
                    "history": {
                        "messages": {
                            "u1": {
                                "id": "u1",
                                "parentId": None,
                                "childrenIds": ["a1"],
                                "role": "user",
                                "content": "Translate hello into French.",
                            },
                            "a1": {
                                "id": "a1",
                                "parentId": "u1",
                                "childrenIds": [],
                                "role": "assistant",
                                "content": answer,
                                "model": model,
                                "done": True,
                            },
                        },
                        "currentId": "a1",
                    },
                }
            },
        )
        assert saved["status"] == 200
        chat_id = saved["body"]["id"]
        page.goto(urljoin(BASE_URL, "c/" + chat_id), wait_until="networkidle")
        page.reload(wait_until="networkidle")
        assert "bonjour" in page.locator("body").inner_text().lower()
        file_id = upload_files(page)
        presentation = stream(
            page,
            model,
            "Create a PPTX presentation about blue squares with three slides.",
        )
        import re

        links = re.findall(r"\]\(([^)]+\.pptx)\)", presentation)
        assert links, "no presentation download in answer"
        download = context.request.get(
            urljoin(BASE_URL, links[0]), headers=auth_header()
        )
        assert download.ok, f"download status {download.status}"
        with ZipFile(BytesIO(download.body())) as deck:
            assert "ppt/presentation.xml" in deck.namelist()
            slides = [
                name
                for name in deck.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ]
            assert len(slides) == 3
            assert any(b"blue" in deck.read(name).lower() for name in slides)
        bob_context = browser.new_context(http_credentials=credentials(True))
        bob_context.set_extra_http_headers(auth_header(True))
        bob = signin(bob_context, True)
        assert bob["id"] != alice["id"]
        bob_page = open_page(bob_context, bob)
        listing = api(bob_page, "/api/v1/chats/")
        assert listing["status"] == 200 and listing["body"] == []
        assert api(bob_page, "/api/v1/chats/" + chat_id)["status"] in (401, 403, 404)
        denied = bob_context.request.get(
            urljoin(BASE_URL, "api/v1/files/" + file_id + "/content"),
            headers=auth_header(True),
        )
        assert denied.status in (401, 403, 404)
        assert_same_origin(page)
        page.screenshot(path=OUTPUT / "alice.png", full_page=False)
        statuses = page.evaluate("""() => Promise.all(Array.from({length: 30}, async () => {
            const response = await fetch('/api/chat/completions', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: '{}'});
            return response.status;
        }))""")
        assert any(status in (429, 503) for status in statuses)
        signout = context.request.post(
            urljoin(BASE_URL, "api/v1/auths/signout"), headers=auth_header()
        )
        assert signout.ok
        page.evaluate("localStorage.removeItem('token')")
        page.reload(wait_until="domcontentloaded")
        assert page.url.startswith(BASE_URL)
        evidence = {
            "basic_401": True,
            "alice_role": alice["role"],
            "identity_overridden": True,
            "signup_403": True,
            "streamed_answer": True,
            "large_image_and_pdf": True,
            "presentation_slides": len(slides),
            "websocket": True,
            "bob_isolated": True,
            "upload_isolated": True,
            "robots": True,
            "rate_limited": True,
            "same_origin": True,
            "deep_reload": True,
            "signout": True,
        }
        (OUTPUT / "browser.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence), flush=True)
        browser.close()


if __name__ == "__main__":
    main()
