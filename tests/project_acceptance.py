"""REQ-PROJ-08: live text-input project acceptance on an owned disposable stack.

Binary source acceptance remains in project_mode_browser.py and needs ingestion.
This runner uses real HTTP, model calls, document workers and browser actions.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
from pathlib import Path
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from docx import Document
from playwright.sync_api import Page, expect, sync_playwright

from project_mode_browser import api, dialog

OUTPUT = Path(
    os.environ.get("ATLAS_PROJECT_BROWSER_OUTPUT", "BRAIN/project-acceptance")
)


def project_base(page: Page) -> str:
    query = parse_qs(urlsplit(page.url).query)
    return f"/api/atlas/orgs/{query['org'][0]}/projects/{query['project'][0]}"


def text_files() -> list[Path]:
    paths = [OUTPUT / "Project brief.md", OUTPUT / "Budget.txt"]
    paths[0].write_text(
        "# Orion delivery brief\nThe pilot serves 42 northern sites and 17 southern sites.\n"
        "Delivery is due on 15 December 2026.\n"
    )
    paths[1].write_text(
        "Orion budget\nNorth: 42 sites, EUR 42000.\nSouth: 17 sites, EUR 17000.\nTotal: 59 sites, EUR 59000.\n"
    )
    return paths


def save(receipt: dict[str, Any]) -> None:
    (OUTPUT / "receipt.json").write_text(json.dumps(receipt, indent=2))


def chat(page: Page, receipt: dict[str, Any]) -> None:
    page.locator(".atlas-project-composer input[type=file]").set_input_files(
        [str(path) for path in text_files()]
    )
    expect(page.locator(".atlas-project-notice")).to_have_text("Saved", timeout=60000)
    message = page.get_by_role("textbox", name="Message", exact=True)
    message.fill(
        "Summarize both sources. Include the site counts, total budget, and delivery date. Do not modify originals."
    )
    page.locator("[data-send]").click()
    expect(page.locator("[data-send]")).to_be_enabled(timeout=180000)
    check_chat(page, receipt)


def check_chat(page: Page, receipt: dict[str, Any]) -> None:
    answers = page.locator(".atlas-project-transcript .answer")
    for term in ("42", "17", "59", "December"):
        expect(answers).to_contain_text(term, timeout=10000)
    assert page.locator(".atlas-project-notice").inner_text() in {"", "Saved"}
    answer = answers.inner_text()
    assert "59,000" in answer or "59000" in answer or "59 000" in answer
    assert "2026" in answer
    links = answers.locator('a[href*="/sources/"]')
    assert links.count() >= 2
    for index in range(links.count()):
        assert links.nth(index).inner_text().isdigit()
        assert urlsplit(links.nth(index).get_attribute("href") or "").path.startswith(
            project_base(page) + "/sources/"
        )
    assert "[Source]" not in answer and "]([" not in answer
    assert not re.search(
        r"https?://|rag\.example|\d+\]?(?:\([^)]*\))?\(https?", answer
    ), answer
    links.first.click()
    expect(page.locator(".atlas-side-pane")).to_be_visible()
    expect(page.locator(".atlas-side-content")).to_contain_text("Orion", timeout=30000)
    page.screenshot(path=str(OUTPUT / "chat-source-preview.png"))
    page.evaluate("window.atlasSideView.close()")
    page.screenshot(path=str(OUTPUT / "chat-and-context.png"))
    receipt.update(chat_useful=True, url=page.url)
    save(receipt)


def work(page: Page, receipt: dict[str, Any]) -> None:
    base = project_base(page)
    sources = api(page, base + "/files")["items"]
    budget = next(file for file in sources if file["relative_path"] == "Budget.txt")
    versions = {file["id"]: file["version_id"] for file in sources}
    for name in ("Project brief.md", "Budget.txt"):
        page.locator(".atlas-project-file").filter(has_text=name).locator(
            "input"
        ).check()
    page.locator("[data-action=work]").click()
    expect(page.locator("[data-send]")).to_be_enabled(timeout=30000)
    request = (
        "Create Orion-memo.docx in project deliverables, a short delivery memo with site counts, budget and date from both selected Markdown/text sources. "
        "First ask me through request_approval whether to delete Budget.txt: "
        + json.dumps(
            {
                "effect_kind": "delete_file",
                "file_id": budget["id"],
                "base_version": budget["version_id"],
                "revision": budget["revision"],
                "maximum_bytes": budget["size_bytes"],
            }
        )
        + ". Wait for my decision. If I deny deletion, preserve the source and continue. "
        "Use read_document on both immutable sources and publish_document to create the DOCX with both declared sources and every source text segment quoted in an appendix. "
        "Do not change any original file. Verify the new document, then finish with a checked recap and canonical download link."
    )
    page.get_by_role("textbox", name="Message", exact=True).fill(request)
    page.locator("[data-send]").click()
    card = page.locator(".atlas-project-activity [data-approval]").last
    expect(card.get_by_role("button", name="Deny", exact=True)).to_be_enabled(
        timeout=180000
    )
    card.scroll_into_view_if_needed()
    page.screenshot(path=str(OUTPUT / "work-approval.png"))
    approval_id = card.get_attribute("data-approval")
    card.get_by_role("button", name="Deny", exact=True).click()
    expect(card).to_contain_text("Decision recorded", timeout=30000)
    expect(page.locator("[data-send]")).to_be_enabled(timeout=300000)
    session_id = parse_qs(urlsplit(page.url).query)["session"][0]
    session = api(page, base + "/work-sessions/" + session_id)
    assert session["state"] == "completed", session["state"]
    assert any(
        value["id"] == approval_id and value["decision"] == "denied"
        for value in session["pending_approvals"]
    )
    items = api(page, base + "/deliverables")["items"]
    docx = next(
        item
        for item in items
        if item["mime"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert docx["validation"]["status"] == "passed"
    current = api(page, base + "/files")["items"]
    assert all(
        next(file for file in current if file["id"] == key)["version_id"] == version
        for key, version in versions.items()
    )
    response = page.request.get(
        urlsplit(page.url)._replace(path=docx["download_route"], query="").geturl()
    )
    assert response.status == 200
    data = response.body()
    assert hashlib.sha256(data).hexdigest() == docx["sha256"]
    document = Document(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    for term in ("42", "17", "59", "2026", "December", "42000", "17000"):
        assert term in text, term
    (OUTPUT / "Orion-memo.docx").write_bytes(data)
    receipt.update(
        work_useful=True,
        approval_denied=True,
        work_session=session_id,
        docx_id=docx["id"],
        docx_sha256=docx["sha256"],
        docx_bytes=len(data),
        url=page.url,
    )
    save(receipt)
    page.locator('[data-ledger-tab="model"]').click()
    page.get_by_role("tabpanel").scroll_into_view_if_needed()
    page.screenshot(path=str(OUTPUT / "results-and-ledger.png"))
    page.locator(f'[data-deliverable="{docx["id"]}"]').get_by_role(
        "button", name="Preview", exact=True
    ).click()
    expect(page.locator(".atlas-side-pane")).to_be_visible()
    expect(page.locator(".atlas-side-content")).to_contain_text("Orion", timeout=30000)
    page.screenshot(path=str(OUTPUT / "docx-preview.png"))
    page.evaluate("window.atlasSideView.close()")


def cancellation(page: Page, receipt: dict[str, Any]) -> None:
    base = project_base(page)
    budget = next(
        file
        for file in api(page, base + "/files")["items"]
        if file["relative_path"] == "Budget.txt"
    )
    request = (
        "Ask through request_approval whether to delete Budget.txt, then wait for my decision. Do not change any file: "
        + json.dumps(
            {
                "effect_kind": "delete_file",
                "file_id": budget["id"],
                "base_version": budget["version_id"],
                "revision": budget["revision"],
                "maximum_bytes": budget["size_bytes"],
            }
        )
    )
    page.get_by_role("textbox", name="Message", exact=True).fill(request)
    page.locator("[data-send]").click()
    card = page.locator(".atlas-project-activity [data-approval]").last
    expect(card.get_by_role("button", name="Deny", exact=True)).to_be_enabled(
        timeout=180000
    )
    session = parse_qs(urlsplit(page.url).query)["session"][0]
    page.locator("[data-stop]").click()
    expect(page.locator("[data-send]")).to_be_enabled(timeout=60000)
    deadline = time.monotonic() + 30
    state = api(page, base + "/work-sessions/" + session)
    while state["state"] == "cancelling" and time.monotonic() < deadline:
        page.wait_for_timeout(500)
        state = api(page, base + "/work-sessions/" + session)
    assert state["state"] == "cancelled", state["state"]
    assert (
        next(
            file
            for file in api(page, base + "/files")["items"]
            if file["id"] == budget["id"]
        )["version_id"]
        == budget["version_id"]
    )
    receipt["cancelled_session"] = session
    receipt["url"] = page.url
    save(receipt)
    page.screenshot(path=str(OUTPUT / "cancelled-work.png"))


def dismiss_release_notes(page: Page) -> None:
    release = page.get_by_role("button", name="Okay, Let's Go!", exact=True)
    if release.is_visible():
        release.click()


def robustness(page: Page, receipt: dict[str, Any]) -> None:
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-send]")).to_be_enabled(timeout=30000)
    before = page.locator("[data-event-seq]").count()
    page.wait_for_timeout(2000)
    seqs = page.locator("[data-event-seq]").evaluate_all(
        "nodes => nodes.map(node => node.dataset.eventSeq)"
    )
    assert len(seqs) == len(set(seqs)) and len(seqs) >= before
    for language, send in (
        ("fr", "Envoyer"),
        ("de", "Senden"),
        ("es", "Enviar"),
        ("it", "Invia"),
        ("en", "Send"),
    ):
        page.evaluate("language => localStorage.setItem('locale',language)", language)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("[data-send]")).to_have_text(send, timeout=30000)
    dismiss_release_notes(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("header .atlas-project-drawer").filter(has_text="Context").click()
    expect(page.locator(".atlas-project-right")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".atlas-project-right")).not_to_be_visible()
    page.screenshot(path=str(OUTPUT / "mobile.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 960})
    receipt.update(
        reload_dedup=True, languages=["en", "fr", "de", "es", "it"], mobile=True
    )
    save(receipt)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--navigation-only", action="store_true")
    parser.add_argument("--work-only", action="store_true")
    parser.add_argument("--cancel-only", action="store_true")
    parser.add_argument("--resume-chat", action="store_true")
    args = parser.parse_args()
    base = os.environ["ATLAS_PROJECT_BROWSER_URL"].rstrip("/")
    target = urlsplit(base)
    assert target.hostname in {"127.0.0.1", "localhost"}
    assert target.port not in {None, 8000, 8020, 3000, 5432}
    account = json.loads(Path(os.environ["ATLAS_PROJECT_BROWSER_ACCOUNT"]).read_text())
    OUTPUT.mkdir(parents=True, exist_ok=True)
    receipt = (
        json.loads((OUTPUT / "receipt.json").read_text())
        if args.work_only or args.cancel_only or args.resume_chat
        else {}
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 960}, locale="en-US"
        )
        errors: list[str] = []
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            response = context.request.post(
                base + "/api/v1/auths/signin",
                data={key: account[key] for key in ("email", "password")},
            )
            assert response.status == 200
            token = response.json()["token"]
            page.goto(base, wait_until="domcontentloaded")
            page.evaluate(
                "token => {localStorage.setItem('token',token); localStorage.setItem('locale','en');}",
                token,
            )
            page.goto(receipt.get("url", base), wait_until="domcontentloaded")
            if not args.work_only and not args.cancel_only and not args.resume_chat:
                page.locator("#chat-input").wait_for(timeout=60000)
                dismiss_release_notes(page)
                page.keyboard.press("Escape")
                page.locator("#atlas-project-entry").click()
                dialog(page, "New project", "Orion acceptance " + str(int(time.time())))
                dialog(page, "New session", "Document comparison")
                expect(page.locator("[data-send]")).to_be_enabled(timeout=30000)
                receipt["url"] = page.url
                save(receipt)
                page.screenshot(path=str(OUTPUT / "navigation.png"))
                if args.navigation_only:
                    robustness(page, receipt)
                    print(
                        "Real project navigation, reload, five locales and mobile: PASS locally"
                    )
                    return
                chat(page, receipt)
            else:
                expect(page.locator("[data-send]")).to_be_enabled(timeout=30000)
            if args.resume_chat:
                check_chat(page, receipt)
            if not args.cancel_only:
                work(page, receipt)
            cancellation(page, receipt)
            robustness(page, receipt)
            receipt["browser_errors"] = errors
            assert not errors, errors
            print(
                "Live scoped text Chat, Work DOCX, denial, previews, ledger and robustness: PASS locally"
            )
        finally:
            receipt["browser_errors"] = errors
            save(receipt)
            page.screenshot(path=str(OUTPUT / "last-state.png"))
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
