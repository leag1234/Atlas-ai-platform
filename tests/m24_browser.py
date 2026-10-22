"""Record computed styles and screenshots from a real saved Open WebUI answer.

Run with ATLAS_RENDER_CHAT_URL pointing at the local saved answer. Requires the
existing development Chromium/Playwright installation, never a synthetic page.
"""

from playwright.sync_api import sync_playwright
import json
import hashlib
import os
from pathlib import Path

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 1280, "height": 1800})
    page.goto(os.environ["ATLAS_RENDER_CHAT_URL"], wait_until="networkidle")
    page.wait_for_timeout(4000)
    prose = page.locator(".chat-assistant .markdown-prose")
    prose.wait_for()
    code = page.locator(".cm-editor,.markdown-prose pre").first
    code_line = page.locator(".cm-line,.markdown-prose pre").first
    page.locator(
        'link[href="/static/custom.css"],link[href="/static/atlas/chat-ui.css"]'
    ).evaluate_all("links => links.forEach(link => link.disabled = true)")
    page.locator(".chat-assistant").screenshot(path="reports/m24-rendering-before.png")
    page.locator(
        'link[href="/static/custom.css"],link[href="/static/atlas/chat-ui.css"]'
    ).evaluate_all("links => links.forEach(link => link.disabled = false)")
    page.mouse.move(0, 0)
    page.wait_for_timeout(500)
    evidence = {
        "font": prose.evaluate("(e)=>getComputedStyle(e).fontFamily"),
        "paragraph_width": prose.locator("p").first.evaluate(
            "(e)=>getComputedStyle(e).maxWidth"
        ),
        "gutters": "none"
        if page.locator(".cm-gutters").count() == 0
        else page.locator(".cm-gutters").first.evaluate(
            "(e)=>getComputedStyle(e).display"
        ),
        "copy": page.locator(".copy-code-button").first.evaluate(
            "(e)=>getComputedStyle(e).opacity"
        ),
        "save": page.get_by_text("Save", exact=True).first.evaluate(
            "(e)=>getComputedStyle(e).display"
        ),
        "wrap": code_line.evaluate("(e)=>getComputedStyle(e).whiteSpace"),
        "css_sha256": hashlib.sha256(
            Path("services/orchestrator/chat-ui.css").read_bytes()
        ).hexdigest(),
    }
    print(json.dumps(evidence), flush=True)
    assert "sans-serif" in evidence["font"].lower()
    assert "serif" not in evidence["font"].lower().replace("sans-serif", "")
    assert evidence["paragraph_width"] == "700px"
    assert evidence["gutters"] == "none"
    assert evidence["copy"] == "0"
    assert evidence["save"] == "none"
    assert evidence["wrap"] == "pre"
    page.locator(".chat-assistant").screenshot(path="reports/m24-rendering-after.png")
    code.hover()
    page.wait_for_timeout(300)
    print(
        "hover",
        page.locator(".copy-code-button").first.evaluate(
            '(e)=>({opacity:getComputedStyle(e).opacity,ancestors:[...document.querySelectorAll(".markdown-prose div:has(> div > div > .copy-code-button)")].map(x=>({hover:x.matches(":hover"),html:x.className}))})'
        ),
        flush=True,
    )
    assert (
        page.locator(".copy-code-button").first.evaluate(
            "(e)=>getComputedStyle(e).opacity"
        )
        == "1"
    )
    page.mouse.move(0, 0)
    page.evaluate(
        'localStorage.setItem("theme", "dark"); localStorage.setItem("atlas-ui", JSON.stringify({style:"arrondi",mode:"dark",accent:"green",size:"16"}))'
    )
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.locator(".chat-assistant").screenshot(path="reports/m24-rendering-dark.png")
    evidence["dark_font"] = prose.evaluate("(e)=>getComputedStyle(e).fontFamily")
    evidence["dark_color"] = prose.evaluate("(e)=>getComputedStyle(e).color")
    print("dark_color", evidence["dark_color"], flush=True)
    assert evidence["dark_color"] == "rgb(245, 245, 247)"
    evidence["copy_on_hover"] = True
    evidence["mode"] = "local-ui"
    evidence["screenshots"] = {
        name: hashlib.sha256(Path("reports", name).read_bytes()).hexdigest()
        for name in (
            "m24-rendering-before.png",
            "m24-rendering-after.png",
            "m24-rendering-dark.png",
        )
    }
    Path("reports/m24-rendering.json").write_text(json.dumps(evidence, indent=2) + "\n")
    b.close()
