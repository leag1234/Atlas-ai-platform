"""Offline Playwright regression for rendered SVG diagrams in the real UI shell.

Run with ATLAS_RENDER_CHAT_URL pointed at the local Open WebUI stack.  The fixture is
inserted into that shell after it loads, matching the rendered Mermaid SVG structure
instead of replacing Open WebUI with a synthetic standalone page.
"""

import os
from pathlib import Path

from playwright.sync_api import Page, TimeoutError, sync_playwright


URL = os.environ["ATLAS_RENDER_CHAT_URL"]
REPORTS = Path("reports")
WIDE_SEQUENCE = """
<div class="mermaid" data-diagram-kind="sequence">
  <div class="diagram-native-actions">
    <button aria-label="Download diagram">Download</button>
    <button aria-label="Regenerate diagram">Regenerate</button>
    <button aria-label="Copy diagram">Copy</button>
  </div>
  <svg viewBox="0 0 2400 720" width="2400" height="720" role="img" aria-label="Wide sequence diagram">
    <rect width="2400" height="720" fill="#fff"/><rect width="2400" height="110" fill="#dceeff"/>
    <text x="70" y="68" font-size="32">Phase one: preparation with an intentionally long label</text>
    <text x="1370" y="68" font-size="32">Phase two: validated response and delivery</text>
    <g stroke="#263238" stroke-width="3" stroke-dasharray="10 8">
      <path d="M180 130V680"/><path d="M760 130V680"/><path d="M1340 130V680"/><path d="M1920 130V680"/>
    </g>
    <g font-size="30" fill="#111"><text x="80" y="115">Client</text><text x="650" y="115">Atlas</text><text x="1200" y="115">Planner</text><text x="1770" y="115">Renderer</text></g>
    <g stroke="#1f7534" stroke-width="5" fill="none"><path d="M180 220H760l-24-14m24 14-24 14"/><path d="M760 340H1340l-24-14m24 14-24 14"/><path d="M1340 460H1920l-24-14m24 14-24 14"/><path d="M1920 580H180l24-14m-24 14 24 14"/></g>
    <g font-size="27" fill="#111"><text x="240" y="205">request a diagram with four lifelines and long labels</text><text x="810" y="325">compose sequence rendering instructions</text><text x="1390" y="445">draw the complete diagram without clipping</text><text x="450" y="565">return accessible SVG with all content visible</text></g>
  </svg>
</div>
<div class="mermaid" data-diagram-kind="flowchart">
  <svg viewBox="0 0 1200 300" width="1200" height="300" role="img" aria-label="Flowchart"><rect width="1200" height="300" fill="#fff"/><rect x="30" y="90" width="250" height="100" fill="#dceeff"/><rect x="470" y="90" width="250" height="100" fill="#dceeff"/><rect x="900" y="90" width="250" height="100" fill="#dceeff"/><path d="M280 140H470M720 140H900" stroke="#1f7534" stroke-width="6"/><text x="80" y="150" font-size="32">Input</text><text x="510" y="150" font-size="32">Render</text><text x="940" y="150" font-size="32">Viewer</text></svg>
</div>
<details data-activity-fixture><summary>Web search<svg class="icon-chevron" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></summary><p>Consulted pages</p></details>
<div class="mermaid" data-diagram-kind="styled"><svg id="mermaid-fixture-1" viewBox="0 0 800 200" width="800" height="200" role="img" aria-label="Styled flow"><style>#mermaid-fixture-1 .flowchart-link{stroke:rgb(51, 51, 51);stroke-width:2px;fill:none}#mermaid-fixture-1 .edgeLabel rect{fill:rgb(232, 232, 232)}</style><path class="flowchart-link" d="M40 100H760"/><g class="edgeLabel"><rect x="340" y="86" width="120" height="28"/><text x="400" y="105" text-anchor="middle">edge label</text></g></svg></div>
"""

USER_MESSAGES = """
<button type="button" data-user-copy-before>Before user messages</button>
<div class="user-message" data-user-copy="plain"><div class="chat-user"><div class="w-full"><div class="flex"><div class="markdown-prose"><p>Copy this exact plain message.</p></div></div></div></div></div>
<div class="user-message" data-user-copy="attachment"><div class="chat-user"><div class="mb-1"><div class="self-end"><button type="button"><span class="truncate">public-notes.pdf</span></button></div></div><div class="w-full"><div class="flex"><div class="markdown-prose"><p>Copy the attachment message only.</p></div></div></div></div></div>
<div class="user-message" data-user-copy="long"><div class="chat-user"><div class="w-full"><div class="flex"><div class="markdown-prose"><p>Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available. Long user message keeps its copy control while the reading toggle remains available.</p></div></div></div></div></div>
<div class="msg-wrap" data-user-copy-wrap><div class="user-message" data-user-copy="native"><div class="chat-user"><div class="w-full"><div class="flex"><div class="markdown-prose"><p>Native copy remains a single action.</p></div></div></div><div class="flex"><button type="button" class="hover-reveal" aria-label="Copy">Copy</button></div></div></div></div>
<style data-user-copy>.msg-wrap{content-visibility:auto}.hover-reveal{opacity:0;pointer-events:none}</style>
"""


def install_fixture(page: Page) -> None:
    page.evaluate(
        """html => {
          document.querySelectorAll('[data-atlas-diagram-fixture]').forEach(node => node.remove());
          const host = document.querySelector('#messages-container') || document.body;
          host.insertAdjacentHTML('beforeend', `<div class="chat-assistant" data-atlas-diagram-fixture><div class="markdown-prose">${html}</div></div>`);
          const fixture = document.querySelector('[data-atlas-diagram-fixture]');
          fixture.querySelector('[aria-label="Download diagram"]').addEventListener('click', () => {
            const link = document.createElement('a');
            link.href = URL.createObjectURL(new Blob(['diagram'], {type: 'image/svg+xml'})); link.download = 'diagram.svg'; link.click(); URL.revokeObjectURL(link.href);
          });
          fixture.querySelector('[aria-label="Regenerate diagram"]').addEventListener('click', () => fixture.dataset.regenerated = 'yes');
          fixture.querySelector('[aria-label="Copy diagram"]').addEventListener('click', () => navigator.clipboard.writeText(fixture.querySelector('svg').outerHTML));
        }""",
        WIDE_SEQUENCE,
    )


def install_user_messages(page: Page) -> None:
    page.evaluate(
        """html => {
          document.querySelectorAll('[data-user-copy],[data-user-copy-wrap]').forEach(node => node.remove());
          const host = document.querySelector('#messages-container') || document.body;
          host.insertAdjacentHTML('beforeend', html);
          document.querySelector('[data-user-copy="native"] button[aria-label="Copy"]')
            .addEventListener('click', event => navigator.clipboard.writeText(
              event.currentTarget.closest('.user-message').querySelector('.markdown-prose').innerText));
        }""",
        USER_MESSAGES,
    )


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=URL)
    page = context.new_page()
    page.goto(URL, wait_until="networkidle")
    try:
        page.get_by_text("Okay, Let's Go!", exact=True).click(timeout=2_000)
    except TimeoutError:
        pass
    install_fixture(page)
    wide = page.locator('[data-diagram-kind="sequence"]')
    wide.wait_for()
    page.wait_for_timeout(150)
    inline_svg = wide.locator("svg")
    metrics = wide.evaluate(
        """host => { const svg = host.querySelector('svg'), column = host.closest('.markdown-prose') || host.parentElement;
          return {width: svg.getBoundingClientRect().width, column: column.getBoundingClientRect().width,
            overflow: host.scrollWidth > host.clientWidth || svg.getBoundingClientRect().right > column.getBoundingClientRect().right + 1}; }"""
    )
    assert metrics["width"] <= metrics["column"] + 1 and not metrics["overflow"], (
        metrics
    )
    # The enhancer adds the expand button asynchronously: wait for it instead of racing it.
    page.locator(
        '[data-diagram-kind="flowchart"] [data-atlas-diagram-expand]'
    ).wait_for(timeout=10_000)
    assert (
        page.locator(
            '[data-diagram-kind="flowchart"] [data-atlas-diagram-expand]'
        ).count()
        == 1
    )
    icon = page.locator("[data-activity-fixture] summary svg")
    assert icon.evaluate("svg => svg.getBoundingClientRect().width") <= 40
    assert (
        page.locator("[data-activity-fixture] [data-atlas-diagram-expand]").count() == 0
    )
    assert "atlas-diagram-svg" not in (icon.get_attribute("class") or "")
    wide.screenshot(path=REPORTS / "diagram-viewer-before.png")
    wide.locator("[data-atlas-diagram-expand]").click()
    viewer = page.locator("#atlas-diagram-viewer")
    viewer.wait_for()
    assert viewer.locator('[role="dialog"][aria-modal="true"]').count() == 1
    viewer_width = viewer.locator(".atlas-diagram-full-svg").evaluate(
        "svg => svg.getBoundingClientRect().width"
    )
    assert viewer_width >= metrics["width"] * 2 - 1, (viewer_width, metrics)
    assert viewer.locator(".atlas-diagram-canvas").evaluate(
        "e => e.scrollWidth > e.clientWidth"
    )
    viewer.screenshot(path=REPORTS / "diagram-viewer-after.png")
    with page.expect_download() as download:
        viewer.locator(".atlas-diagram-download").click()
    assert download.value.suggested_filename == "diagram.svg"
    viewer.locator(".atlas-diagram-copy").click()
    page.wait_for_timeout(100)
    assert "Wide sequence diagram" in page.evaluate("navigator.clipboard.readText()")
    viewer.locator(".atlas-diagram-regenerate").click()
    assert (
        page.locator("[data-atlas-diagram-fixture]").get_attribute("data-regenerated")
        == "yes"
    )
    assistant_actions = page.locator(
        "[data-atlas-diagram-fixture] .diagram-native-actions button"
    ).count()
    # Close the viewer first: the user-message checks need the page beneath it.
    page.keyboard.press("Escape")
    assert viewer.count() == 0
    install_user_messages(page)
    page.wait_for_timeout(150)
    for name, expected in (
        ("plain", "Copy this exact plain message."),
        ("attachment", "Copy the attachment message only."),
    ):
        bubble = page.locator(f'[data-user-copy="{name}"]')
        copy = bubble.locator(".atlas-user-copy-button")
        assert copy.count() == 1
        copy.click()
        assert page.evaluate("navigator.clipboard.readText()") == expected
        assert copy.get_attribute("data-atlas-copied") == "true"
        assert (
            copy.locator("svg").evaluate(
                "svg => Math.max(svg.getBoundingClientRect().width, svg.getBoundingClientRect().height)"
            )
            <= 24
        )
    plain = page.locator('[data-user-copy="plain"]')
    page.locator("[data-user-copy-before]").focus()
    page.keyboard.press("Tab")
    assert page.evaluate(
        "document.activeElement.classList.contains('atlas-user-copy-button')"
    )
    long = page.locator('[data-user-copy="long"]')
    assert long.locator(".atlas-message-toggle").count() == 1
    long.locator(".atlas-user-copy-button").click()
    assert page.evaluate("navigator.clipboard.readText()").startswith(
        "Long user message"
    )
    native = page.locator('[data-user-copy="native"]')
    assert native.locator(".atlas-user-copy-button").count() == 0
    assert native.locator(".atlas-user-native-copy").count() == 1
    # Real pointer path: from the bubble centre to the native action, then a mouse click.
    # The fixture is pinned near the top of the window (the page itself does not scroll);
    # its content-visibility wrapper stays, because that wrapper is what clipped the action.
    page.locator("[data-atlas-diagram-fixture]").evaluate(
        "e => { e.style.visibility = 'hidden'; }"
    )
    page.locator("[data-user-copy-wrap]").evaluate(
        "e => Object.assign(e.style, {position: 'fixed', top: '60px', left: '10px', width: '360px', zIndex: '100000', background: '#fff'})"
    )
    page.wait_for_timeout(300)
    bubble_box = native.locator(".markdown-prose").bounding_box()
    action_box = native.locator(".atlas-user-native-copy").bounding_box()
    assert bubble_box and action_box
    page.mouse.move(
        bubble_box["x"] + bubble_box["width"] / 2,
        bubble_box["y"] + bubble_box["height"] / 2,
    )
    page.mouse.move(
        action_box["x"] + action_box["width"] / 2,
        action_box["y"] + action_box["height"] / 2,
        steps=8,
    )
    centre = [
        action_box["x"] + action_box["width"] / 2,
        action_box["y"] + action_box["height"] / 2,
    ]
    hit = page.evaluate(
        "p => { const e = document.elementFromPoint(p[0], p[1]); return e ? e.closest('.atlas-user-native-copy') !== null : null; }",
        centre,
    )
    assert hit is True, (
        hit,
        centre,
        page.viewport_size,
        page.evaluate(
            "p => document.elementsFromPoint(p[0], p[1]).slice(0, 4).map(e => e.tagName + '.' + String(e.className).slice(0, 30))",
            centre,
        ),
    )
    page.mouse.click(
        action_box["x"] + action_box["width"] / 2,
        action_box["y"] + action_box["height"] / 2,
    )
    assert (
        page.evaluate("navigator.clipboard.readText()")
        == "Native copy remains a single action."
    )
    assert (
        page.locator(
            "[data-atlas-diagram-fixture] .diagram-native-actions button"
        ).count()
        == assistant_actions
    )
    page.locator("[data-atlas-diagram-fixture]").evaluate(
        "e => { e.style.visibility = ''; }"
    )
    inline_svg.scroll_into_view_if_needed()
    inline_svg.click()
    viewer.wait_for()
    viewer.locator(".atlas-diagram-close").focus()
    page.keyboard.press("Tab")
    assert page.evaluate(
        "document.activeElement.classList.contains('atlas-diagram-zoom-out')"
    )
    # On a phone width the viewer is full screen: click-outside is checked on a desktop width.
    page.set_viewport_size({"width": 1280, "height": 800})
    page.mouse.click(2, 400)
    assert viewer.count() == 0
    # Mermaid scopes its styles to the SVG id: the enlarged copy must keep strokes and label fills.
    styled = page.locator('[data-diagram-kind="styled"]')
    styled.scroll_into_view_if_needed()
    styled.locator("[data-atlas-diagram-expand]").click()
    viewer.wait_for()
    copy_styles = viewer.locator(".atlas-diagram-full-svg").evaluate(
        "svg => [getComputedStyle(svg.querySelector('.flowchart-link')).stroke, getComputedStyle(svg.querySelector('.edgeLabel rect')).fill]"
    )
    assert copy_styles == ["rgb(51, 51, 51)", "rgb(232, 232, 232)"], copy_styles
    page.keyboard.press("Escape")
    assert viewer.count() == 0
    context.close()
    browser.close()
