"""Side-view acceptance on the real local Open WebUI shell; --live spends one call."""

import base64
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from playwright.sync_api import Page, expect, sync_playwright

from packages.ports import atlas_ports
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.documents import Attachment
from services.orchestrator.side_view import pdf_placeholders
from test_m23_documents import pdf

URL = os.environ.get("ATLAS_RENDER_CHAT_URL", atlas_ports().url("chat_ui"))
OUTPUT = Path("BRAIN/side-view")


def api(page: Page, path: str, body: dict[str, Any] | None = None) -> Any:
    result = page.evaluate(
        """async ({path, body}) => { const r = await fetch(path, {method: body ? 'POST' : 'GET',
          headers: {'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.token}`},
          ...(body ? {body: JSON.stringify(body)} : {})}); return {status: r.status, body: await r.json()}; }""",
        {"path": path, "body": body},
    )
    assert result["status"] == 200, result["status"]
    return result["body"]


def save_chat(page: Page, answer: str, model: str) -> None:
    user = {
        "id": "side-user",
        "parentId": None,
        "childrenIds": ["side-answer"],
        "role": "user",
        "content": "Read beside chat",
        "timestamp": int(time.time()),
    }
    assistant = {
        "id": "side-answer",
        "parentId": "side-user",
        "childrenIds": [],
        "role": "assistant",
        "content": answer,
        "model": model,
        "done": True,
        "timestamp": int(time.time()),
    }
    chat = api(
        page,
        "/api/v1/chats/new",
        {
            "chat": {
                "title": "Side view acceptance",
                "models": [model],
                "messages": [user, assistant],
                "history": {
                    "messages": {user["id"]: user, assistant["id"]: assistant},
                    "currentId": assistant["id"],
                },
                "params": {},
            }
        },
    )
    page.goto(URL + "/c/" + chat["id"], wait_until="networkidle")
    page.locator(".chat-assistant .markdown-prose").wait_for()


def upload(page: Page, data: bytes) -> str:
    result = page.evaluate(
        """async encoded => { const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
          const form = new FormData(); form.append('file', new File([bytes], 'side-view.pdf', {type:'application/pdf'}));
          const r = await fetch('/api/v1/files/?process=false', {method:'POST', headers:{Authorization:`Bearer ${localStorage.token}`}, body:form});
          return {status:r.status, body:await r.json()}; }""",
        base64.b64encode(data).decode(),
    )
    assert result["status"] == 200, result["status"]
    file_id = str(result["body"]["id"])
    # No Authorization header: this proves same-origin session-cookie access.
    status = page.evaluate(
        "async id => (await fetch(`/api/v1/files/${id}/content`, {credentials:'same-origin'})).status",
        file_id,
    )
    assert status == 200, status
    return file_id


def assert_layout(page: Page, width: int, state: str) -> dict[str, Any]:
    """Measure the actual message list, not a wider ancestor hiding unused space."""
    page.set_viewport_size({"width": width, "height": 1000})
    page.wait_for_timeout(350)  # Let the native width transition and resize settle.
    geometry = page.evaluate(
        """() => {
          const rect = selector => {
            const e = document.querySelector(selector), r = e.getBoundingClientRect();
            return {x:r.x, right:r.right, width:r.width, display:getComputedStyle(e).display};
          };
          const pane = rect('#atlas-side-view');
          const overlaps = [...document.querySelectorAll(
            '.atlas-app-root .app, #sidebar, #main-content, #chat-container, #chat-pane, #messages-container, #controls-container, #controls-resizer'
          )].filter(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.right > pane.x + 1;
          }).map(e => e.id || e.className);
          return {pane, messages:rect('#messages-container'), sidebar:rect('#sidebar'), overlaps};
        }"""
    )
    minimum = width - geometry["pane"]["width"] - geometry["sidebar"]["width"] - 40
    assert geometry["messages"]["width"] >= minimum, (state, width, minimum, geometry)
    assert geometry["overlaps"] == [], (state, width, geometry)
    assert abs(geometry["pane"]["right"] - width) <= 1, geometry
    return {"viewport": width, "state": state, **geometry}


def layout_checks(page: Page, src: str) -> None:
    measurements = []
    for width in (1280, 1500, 1920):
        page.evaluate("window.atlasSideView.close()")
        # The fixture's HTML block opens Open WebUI's real native artifacts pane.
        expect(page.locator("#controls-container")).to_be_visible()
        expect(
            page.frame_locator("#artifacts-container iframe").locator("h1")
        ).to_have_text("Local preview")
        page.evaluate(
            "window.atlasSideView.open({kind:'markdown',title:'Layout notes',content:'# Side notes\\n\\nBudget: **730 euros**.'})"
        )
        measurements.append(assert_layout(page, width, "native-artifact"))
        expect(page.locator("#controls-container")).to_be_hidden()
        page.screenshot(path=str(OUTPUT / f"layout-{width}.png"))
        page.get_by_role("button", name="Split", exact=True).click()
        page.evaluate(
            "src => window.atlasSideView.open({kind:'pdf',src,title:'Two page PDF',slot:'bottom'})",
            src,
        )
        expect(
            page.locator('[data-slot="bottom"] canvas[data-rendered="true"]')
        ).to_have_count(2)
        measurements.append(assert_layout(page, width, "split"))
        page.screenshot(path=str(OUTPUT / f"layout-{width}-split.png"))
        # Resizing must obey the same contract.
        page.locator(".atlas-side-grip").focus()
        page.keyboard.press("ArrowLeft")
        measurements.append(assert_layout(page, width, "resized-split"))
    desktop_width = page.locator("#atlas-side-view").evaluate(
        "e => e.getBoundingClientRect().width"
    )
    for width in (899, 600, 375):
        page.set_viewport_size({"width": width, "height": 1000})
        expect(page.locator("#atlas-side-view")).to_have_css("position", "fixed")
        box = page.locator("#atlas-side-view").bounding_box()
        assert box and box["x"] == 0 and box["width"] == width, box
        close = page.get_by_role("button", name="Close side view", exact=True)
        expect(close).to_be_in_viewport()
        expect(page.locator("#artifacts-container")).to_be_hidden()
        page.screenshot(path=str(OUTPUT / f"layout-{width}-overlay.png"))
        close.click()
        expect(page.locator("#atlas-side-view")).to_be_hidden()
        page.evaluate(
            "window.atlasSideView.open({kind:'markdown',content:'Overlay reopened'})"
        )
    page.set_viewport_size({"width": 1920, "height": 1000})
    measurements.append(assert_layout(page, 1920, "after-overlay"))
    assert (
        page.locator("#atlas-side-view").evaluate(
            "e => e.getBoundingClientRect().width"
        )
        == desktop_width
    )
    page.evaluate("window.atlasSideView.close()")
    expect(page.locator("#controls-container")).to_be_visible()
    page.locator("#chat-container nav").get_by_role(
        "button", name="Controls", exact=True
    ).click()
    expect(page.locator("#controls-container")).to_have_count(0)
    page.evaluate(
        "window.atlasSideView.open({kind:'markdown',content:'Chat without native panes'})"
    )
    for width in (1280, 1500, 1920):
        measurements.append(assert_layout(page, width, "no-native-pane"))
    page.evaluate("window.atlasSideView.close()")
    page.locator("#chat-container nav").get_by_role(
        "button", name="Controls", exact=True
    ).click()
    expect(page.locator("#controls-container")).to_be_visible()
    page.evaluate(
        "window.atlasSideView.open({kind:'markdown',content:'Chat with native controls'})"
    )
    for width in (1280, 1500, 1920):
        measurements.append(assert_layout(page, width, "native-controls"))
        expect(page.locator("#controls-container")).to_be_hidden()
    page.set_viewport_size({"width": 899, "height": 1000})
    expect(page.locator(".modal")).to_be_hidden()
    page.get_by_role("button", name="Close side view", exact=True).click()
    expect(page.locator("#atlas-side-view")).to_be_hidden()
    expect(page.locator(".modal")).to_be_visible()
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.evaluate("window.atlasSideView.close()")
    expect(page.locator("#controls-container")).to_be_visible()
    page.locator("#chat-container nav").get_by_role(
        "button", name="Controls", exact=True
    ).click()
    (OUTPUT / "layout-measurements.json").write_text(json.dumps(measurements, indent=2))
    page.set_viewport_size({"width": 1500, "height": 1000})


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.goto(URL, wait_until="networkidle")
        welcome = page.get_by_text("Okay, Let's Go!", exact=True)
        if welcome.count():
            welcome.click()
        page.wait_for_function("!!window.atlasSideView")
        model = api(page, "/api/models")["data"][0]["id"]
        raw = pdf(2)
        file_id = upload(page, raw)
        src = f"/api/v1/files/{file_id}/content"
        if "--live" in sys.argv:
            result = page.evaluate(
                """async ({model, fileId}) => {
                  const r = await fetch('/api/chat/completions', {method:'POST', headers:{'Content-Type':'application/json', Authorization:`Bearer ${localStorage.token}`},
                    body:JSON.stringify({model, stream:true, messages:[{role:'user',content:'What is the initial budget in the attached PDF? Answer in one sentence. Do not search.'}], files:[{type:'file',id:fileId}]})});
                  return {status:r.status, text:await r.text()}; }""",
                {"model": model, "fileId": file_id},
            )
            assert result["status"] == 200, result["status"]
            answer = ""
            cost = None
            for line in result["text"].splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    event = json.loads(line[6:])
                    answer += "".join(
                        choice.get("delta", {}).get("content", "")
                        for choice in event.get("choices", [])
                    )
                    cost = event.get("atlas", {}).get("cost_eur", cost)
            assert 'data-atlas-view="pdf"' in answer, "missing real emitted placeholder"
            assert "730" in answer, "missing useful PDF answer"
            (OUTPUT / "live.json").write_text(
                json.dumps({"calls": 1, "cost_eur": cost, "answer": answer}, indent=2)
            )
            save_chat(page, answer, model)
            expect(
                page.locator('#atlas-side-view canvas[data-rendered="true"]')
            ).to_have_count(2)
            page.screenshot(path=str(OUTPUT / "live.png"))
            print(
                "Live: uploaded PDF read; useful budget answer; emitted opener rendered two pages; screenshot saved"
            )
        else:
            payload = ChatRequest(
                messages=[ChatMessage(role="user", content="Read PDF")],
                documents=[
                    Attachment(
                        filename="side-view.pdf",
                        file_data=base64.b64encode(raw).decode(),
                        file_id=file_id,
                    )
                ],
            )
            answer = (
                pdf_placeholders(payload)
                + "\nHere is a document.\n\n```markdown\n# Side notes\n\nBudget: **730 euros**.\n```\n\n```html\n<h1>Local preview</h1>\n```\n"
            )
            save_chat(page, answer, model)
            # The native Markdown fallback rehydrates the span after sanitization.
            expect(
                page.locator('.chat-assistant .atlas-view[data-atlas-view="pdf"]')
            ).to_have_count(1)
            expect(
                page.locator('#atlas-side-view canvas[data-rendered="true"]')
            ).to_have_count(2)
            expect(page.locator(".cm-editor + .atlas-side-chip")).to_have_count(2)
            layout_checks(page, src)
            page.evaluate("window.atlasSideView.close()")
            page.locator(".cm-editor + .atlas-side-chip").first.click()
            expect(page.locator(".atlas-side-editor")).to_have_value(
                "# Side notes\n\nBudget: **730 euros**."
            )
            page.evaluate("window.atlasSideView.close()")
            page.locator(".chat-assistant .atlas-side-chip").first.click()
            expect(page.locator("#atlas-side-view")).to_be_visible()
            page.evaluate(
                "window.atlasSideView.open({kind:'markdown',title:'Editable notes',content:'# Side notes\\n\\nBudget: **730 euros**.\\n\\n<script>window.sideUnsafe=true</script>\\n[bad](javascript:alert(1))'})"
            )
            expect(page.locator(".atlas-side-preview h1")).to_have_text("Side notes")
            assert (
                page.locator(
                    '.atlas-side-preview script, .atlas-side-preview [href^="javascript:"]'
                ).count()
                == 0
            )
            page.locator(".atlas-side-editor").fill(
                "# Updated notes\n\nTwo views can stay open."
            )
            expect(page.locator(".atlas-side-preview h1")).to_have_text("Updated notes")
            assert page.evaluate(
                "document.querySelector('#atlas-side-view').parentElement === document.querySelector('.atlas-app-root').parentElement"
            )
            page.screenshot(path=str(OUTPUT / "markdown.png"))
            # Width resizing is keyboard-accessible and persisted independently of chat.
            before = page.locator("#atlas-side-view").bounding_box()
            assert before
            page.locator(".atlas-side-grip").focus()
            page.keyboard.press("ArrowLeft")
            after = page.locator("#atlas-side-view").bounding_box()
            assert after and after["width"] > before["width"]
            assert (
                float(page.evaluate("localStorage.getItem('atlas-side-width')"))
                == after["width"]
            )
            page.get_by_role("button", name="Split", exact=True).click()
            page.evaluate(
                "src => window.atlasSideView.open({kind:'pdf',src,title:'Two page PDF',slot:'bottom'})",
                src,
            )
            expect(
                page.locator('[data-slot="bottom"] canvas[data-rendered="true"]')
            ).to_have_count(2)
            expect(page.locator(".atlas-side-page-count")).to_have_text("/ 2")
            page.screenshot(path=str(OUTPUT / "split-pdf.png"))
            page.evaluate(
                "window.atlasSideView.open({kind:'url',src:'https://github.com/',title:'Framing fallback',slot:'bottom'})"
            )
            expect(page.locator(".atlas-side-fallback")).to_be_visible(timeout=6000)
            expect(
                page.get_by_role("button", name="Open in your browser", exact=True)
            ).to_be_visible()
            page.screenshot(path=str(OUTPUT / "url-fallback.png"))
            page.evaluate(
                "window.atlasSideView.open({kind:'html',content:'<h1>Sandboxed preview</h1><script>document.body.dataset.ran=1</script>',title:'HTML',slot:'bottom'})"
            )
            expect(page.frame_locator(".atlas-side-frame").locator("h1")).to_have_text(
                "Sandboxed preview"
            )
            assert (
                page.locator(".atlas-side-frame").get_attribute("sandbox")
                == "allow-scripts"
            )
            page.locator('[data-slot="bottom"]').get_by_role(
                "button", name="Close slot", exact=True
            ).click()
            page.locator('[data-slot="top"]').get_by_role(
                "button", name="Close slot", exact=True
            ).click()
            expect(page.locator("#atlas-side-view")).to_be_hidden()
            page.evaluate(
                "window.atlasSideView.open({kind:'markdown',content:'Escape closes'})"
            )
            page.keyboard.press("Escape")
            expect(page.locator("#atlas-side-view")).to_be_hidden()
            page.evaluate(
                "location.hash = '#atlas-view=url:' + encodeURIComponent(location.origin + '/favicon.png')"
            )
            expect(page.locator(".atlas-side-frame")).to_have_attribute(
                "src", URL + "/favicon.png"
            )
            page.evaluate("document.documentElement.classList.add('dark')")
            assert (
                page.locator("#atlas-side-view").evaluate(
                    "e => getComputedStyle(e).color"
                )
                == "rgb(245, 245, 247)"
            )
            page.evaluate("window.atlasSideView.close()")
            print(
                "Offline real page: sanitized span, reopen chip, markdown editing/sanitizer, split, two PDF canvases, cookie access, URL watchdog, sandboxed HTML, slot close and Escape verified"
            )
        browser.close()


if __name__ == "__main__":
    main()
