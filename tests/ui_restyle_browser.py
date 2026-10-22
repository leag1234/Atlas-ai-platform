"""Capture the owner reference chat through the real Open WebUI browser shell."""

import json
import os
from pathlib import Path
from typing import cast

from playwright.sync_api import (
    Browser,
    Error,
    FilePayload,
    Page,
    TimeoutError,
    sync_playwright,
)

from ui_fidelity_browser import (
    languages,
    measure,
    popover_rows,
    resize_sidebar,
    settings_matrix,
)


URL = os.environ["ATLAS_RENDER_CHAT_URL"]
OUTPUT = Path("reports/ui-restyle")
MEASUREMENTS: dict[str, object] = {}
PLAYWRIGHT_TIMEOUT_MS = 60_000
PASTE_SCREENSHOT_DIR = (
    Path(os.environ["ATLAS_PASTE_SCREENSHOT_DIR"])
    if os.environ.get("ATLAS_PASTE_SCREENSHOT_DIR")
    else None
)
LONG_PASTE = "\n\n".join(
    [
        "Cette note décrit précisément le comportement attendu de la zone de composition et garde une largeur de lecture confortable pour chaque phrase. "
        * 3
        + "Elle doit rester entièrement lisible quand plusieurs paragraphes sont collés dans l’éditeur."
    ]
    * 3
)


def synthetic_images(count: int) -> list[FilePayload]:
    """Return browser-file-chooser payloads for public, synthetic image previews."""
    colors = ("#e84d4d", "#2878d4", "#1f9d55")
    return [
        {
            "name": f"synthetic-composer-{index + 1}.svg",
            "mimeType": "image/svg+xml",
            "buffer": (
                '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
                f'<rect width="96" height="96" fill="{colors[index]}"/>'
                "</svg>"
            ).encode(),
        }
        for index in range(count)
    ]


def synthetic_pdf() -> FilePayload:
    """A deliberately long synthetic name proves the native file-card truncation."""
    return {
        "name": "Synthetic public composer attachment with a deliberately long name for truncation evidence 2026.pdf",
        "mimeType": "application/pdf",
        "buffer": b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF",
    }


def attach(page: Page, files: list[FilePayload]) -> None:
    """Set files on Open WebUI's hidden native input and wait for decoded previews."""
    # The menu creates the real, hidden Upload Files input. Set it directly instead
    # of driving the browser chooser, then wait for Open WebUI to render its queue.
    page.locator("#input-menu-button").click()
    chooser = page.locator('input[type="file"]:not(#camera-input)')
    chooser.wait_for(state="attached")
    chooser.set_input_files(files)
    image_count = sum(file["mimeType"].startswith("image/") for file in files)
    page.wait_for_function(
        """expected => {
          const container = document.querySelector('#message-input-container');
          if (!container) return false;
          const previews = [...container.querySelectorAll('img[data-cy="image"]')];
          const removes = [...container.querySelectorAll(':scope > div:first-of-type button[aria-label]:not(:has(img[data-cy="image"]))')];
          return previews.length === expected.images && removes.length === expected.files
            && previews.every(image => image.naturalWidth > 0);
        }""",
        arg={"files": len(files), "images": image_count},
        timeout=20_000,
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)


def boxes_do_not_intersect(page: Page) -> dict[str, object]:
    """Keep attachment previews clear of the dedicated input row controls."""
    values = page.evaluate(
        """() => {
          const box = element => {
            const r = element.getBoundingClientRect();
            return {left:r.left, right:r.right, top:r.top, bottom:r.bottom,
                    width:r.width, height:r.height};
          };
          const attachments = [
            ...document.querySelectorAll('#message-input-container img[data-cy="image"]'),
            ...document.querySelectorAll('#message-input-container > div:first-of-type button[aria-label]:not(:has(img[data-cy="image"]))'),
          ].map(box);
          const controls = ['#input-menu-button', '#send-message-button', '#chat-input-container']
            .map(selector => document.querySelector(selector)).filter(Boolean).map(box);
          const intersects = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
          return {attachments, controls, collisions: attachments.flatMap(attachment => controls.filter(control => intersects(attachment, control)))};
        }"""
    )
    assert not values["collisions"], values
    return cast(dict[str, object], values)


def composer_capture(
    browser: Browser,
    name: str,
    style: str,
    theme: str,
    size: int,
    width: int,
    files: list[FilePayload],
    screenshot: bool = False,
) -> None:
    """Exercise one isolated native-composer scenario, retrying only that scenario."""
    last_error: Error | AssertionError | None = None
    for attempt in range(3):
        page = browser.new_page(
            viewport={"width": width, "height": 900}, locale="fr-FR"
        )
        try:
            page.goto(URL, wait_until="networkidle")
            try:
                page.get_by_text("Okay, Let's Go!", exact=True).click(timeout=1000)
            except TimeoutError:
                pass
            page.evaluate(
                """([style, theme, size]) => {
                  localStorage.setItem('atlas-ui', JSON.stringify({style, mode: theme, accent: 'green', size: String(size)}));
                  localStorage.setItem('theme', theme);
                }""",
                [style, theme, size],
            )
            page.reload(wait_until="networkidle")
            page.locator("#chat-input").wait_for()
            attach(page, files)
            page.locator("#chat-input").fill("Keep typing normally")
            page.keyboard.press("Shift+Enter")
            page.keyboard.type("with a second line")
            _assert_composer(page, name, style, theme, size, width, files, screenshot)
            return
        except (AssertionError, Error) as error:
            last_error = error
            if attempt == 2:
                raise
        finally:
            page.close()
    raise AssertionError(f"{name} exhausted its bounded retry") from last_error


def long_paste_capture(
    browser: Browser,
    style: str,
    theme: str,
    size: int,
    screenshot: Path | None = None,
) -> None:
    """Dispatch a long multi-paragraph Tiptap input without keyboard or clipboard I/O."""
    page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="fr-FR")
    try:
        # This path must never wait indefinitely for an Open WebUI browser action.
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
        page.set_default_navigation_timeout(PLAYWRIGHT_TIMEOUT_MS)
        page.goto(URL, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        try:
            page.get_by_text("Okay, Let's Go!", exact=True).click(timeout=1000)
        except TimeoutError:
            pass
        page.evaluate(
            """([style, theme, size]) => {
              localStorage.setItem('atlas-ui', JSON.stringify({style, mode: theme, accent: 'green', size: String(size)}));
              localStorage.setItem('theme', theme);
            }""",
            [style, theme, size],
        )
        page.reload(wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        editor = page.locator("#chat-input")
        editor.wait_for()
        page.evaluate(
            """text => {
              const editor = document.querySelector('#chat-input');
              if (!editor) throw new Error('chat input missing');
              const paragraphs = text.split('\\n\\n').map(value => {
                const paragraph = document.createElement('p');
                paragraph.textContent = value;
                return paragraph;
              });
              editor.replaceChildren(...paragraphs);
              editor.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: text,
              }));
            }""",
            LONG_PASTE,
        )
        expand = page.locator('button[aria-label="Expand input"]')
        expand.wait_for()
        values = page.evaluate(
            """() => {
              const box = element => {
                const rect = element.getBoundingClientRect();
                return {left: rect.left, right: rect.right, top: rect.top,
                  bottom: rect.bottom, width: rect.width, height: rect.height};
              };
              const overlaps = (left, right) => left.left < right.right
                && left.right > right.left && left.top < right.bottom
                && left.bottom > right.top;
              const composer = document.querySelector('#message-input-container');
              const cell = composer.querySelector(':scope > .relative:has(#chat-input-container)');
              const text = document.querySelector('#chat-input-container');
              const expand = cell.querySelector('button[aria-label="Expand input"]');
              const allowed = new Set([
                document.querySelector('#input-menu-button'),
                document.querySelector('#send-message-button'),
              ]);
              const controls = [...composer.querySelectorAll('button')]
                .filter(button => button !== null)
                .map(button => ({label: button.getAttribute('aria-label'),
                  allowed: allowed.has(button), expand: button === expand, box: box(button)}));
              return {composer: box(composer), cell: box(cell), text: box(text),
                controls, overlaps: controls.filter(control =>
                  overlaps(control.box, box(text)) && !control.allowed && !control.expand)};
            }"""
        )
        assert values["text"]["width"] >= values["cell"]["width"] * 0.9, values
        assert not values["overlaps"], values
        expand_box = expand.bounding_box()
        assert expand_box is not None
        expand_right = expand_box["x"] + expand_box["width"]
        expand_bottom = expand_box["y"] + expand_box["height"]
        assert expand_right <= values["cell"]["right"] + 1, values
        assert expand_right >= values["cell"]["right"] - 20, values
        assert expand_box["y"] >= values["cell"]["top"], values
        assert expand_box["y"] <= values["cell"]["top"] + 20, values
        assert expand_bottom <= values["cell"]["bottom"], values
        MEASUREMENTS[f"composer-long-paste-{style}-{theme}-{size}-1280"] = values
        if screenshot is not None:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=screenshot, full_page=False, timeout=PLAYWRIGHT_TIMEOUT_MS
            )
    finally:
        page.close()


def _assert_composer(
    page: Page,
    name: str,
    style: str,
    theme: str,
    size: int,
    width: int,
    files: list[FilePayload],
    screenshot: bool,
) -> None:
    """Assert the completed attachment queue before recording one isolated scenario."""
    images = page.locator('#message-input-container img[data-cy="image"]')
    removes = page.locator(
        '#message-input-container > div:first-of-type button[aria-label]:not(:has(img[data-cy="image"]))'
    )
    assert images.count() == sum(
        file["mimeType"].startswith("image/") for file in files
    )
    assert removes.count() == len(files)
    if images.count():
        dimensions = images.evaluate_all(
            "images => images.map(image => { const r = image.getBoundingClientRect(); return [r.width, r.height]; })"
        )
        assert dimensions == [[64, 64]] * images.count(), dimensions
    remove_dimensions = removes.evaluate_all(
        "buttons => buttons.map(button => { const r = button.getBoundingClientRect(); return [r.width, r.height]; })"
    )
    assert all(width >= 44 and height >= 44 for width, height in remove_dimensions)
    file_cards = page.locator("#message-input-container > div:first-of-type > button")
    if any(file["mimeType"] == "application/pdf" for file in files):
        assert file_cards.count() == 1
        card_box = file_cards.first.bounding_box()
        assert card_box is not None and card_box["width"] <= 220
        assert file_cards.first.locator(".truncate.pr-1").count() == 1
    values = boxes_do_not_intersect(page)
    composer = page.locator("#message-input-container").bounding_box()
    assert composer is not None and composer["height"] > 52
    assert page.locator("#message-input-container").evaluate(
        "element => getComputedStyle(element).height !== '52px'"
    )
    assert page.locator("#message-input-container").evaluate(
        "element => Math.round(parseFloat(getComputedStyle(element).borderRadius)) === 24"
    )
    MEASUREMENTS[name] = {
        "style": style,
        "theme": theme,
        "size": size,
        "viewport_width": width,
        "composer": composer,
        "no_input_row_collisions": values,
        "remove_dimensions": remove_dimensions,
    }
    if screenshot:
        page.screenshot(path=OUTPUT / name, full_page=False)


def bubble_fixture(page: Page) -> None:
    """Add synthetic saved-message markup to the real Open WebUI shell."""
    filenames = [
        "SYNTHETIC QUARTERLY RECONCILIATION ARCHIVE 2026 001 ALPHA BRAVO CHARLIE DELTA.pdf",
        "SYNTHETIC COMPLIANCE EVIDENCE REGISTER 2026 002 ECHO FOXTROT GOLF HOTEL.pdf",
        "SYNTHETIC PROJECT DELIVERY RECORD 2026 003 INDIA JULIET KILO LIMA.pdf",
    ]
    pasted_lines = "<br>".join(
        f"Synthetic pasted paragraph line {line:02d} with enough ordinary text to remain readable."
        for line in range(1, 31)
    )
    cards = "".join(
        f'<div class="mb-1"><div class="self-end"><button type="button"><span class="truncate">{name}</span><span class="text-gray-500">1.8 MB</span></button></div></div>'
        for name in filenames
    )
    page.evaluate(
        """([cards, pastedLines]) => {
          document.querySelectorAll('[data-atlas-bubble-fixture]').forEach(node => node.remove());
          const host = document.querySelector('#messages-container') || document.body;
          host.insertAdjacentHTML('beforeend', `<div data-atlas-bubble-fixture class="message-listitem"><div class="user-message"><div class="chat-user">${cards}<div class="w-full"><div class="flex"><div class="rounded-3xl markdown-prose"><p>${pastedLines}</p></div></div></div></div></div></div><div data-atlas-bubble-fixture class="message-listitem"><div class="user-message"><div class="chat-user"><div class="mb-1"><div class="self-end"><button type="button"><span class="truncate">SYNTHETIC SHORT ATTACHMENT 2026.pdf</span><span class="text-gray-500">24 KB</span></button></div></div><div class="w-full"><div class="flex"><div class="rounded-3xl markdown-prose"><p>Short synthetic message.</p></div></div></div></div></div></div>`);
        }""",
        [cards, pasted_lines],
    )


def bubble_capture(
    browser: Browser, style: str, theme: str, size: int, width: int
) -> None:
    """Check one synthetic long-message fixture through the injected browser UI."""
    name = f"bubble-long-{style}-{theme}-{size}-{width}.png"
    page = browser.new_page(viewport={"width": width, "height": 900}, locale="en-US")
    try:
        page.goto(URL, wait_until="networkidle")
        try:
            page.get_by_text("Okay, Let's Go!", exact=True).click(timeout=1000)
        except TimeoutError:
            pass
        page.evaluate(
            """([style, theme, size]) => {
              localStorage.setItem('atlas-ui', JSON.stringify({style, mode: theme, accent: 'green', size: String(size)}));
              localStorage.setItem('theme', theme);
            }""",
            [style, theme, size],
        )
        page.reload(wait_until="networkidle")
        bubble_fixture(page)
        long_bubble = page.locator("[data-atlas-bubble-fixture] .user-message").first
        short_bubble = page.locator("[data-atlas-bubble-fixture] .user-message").nth(1)
        toggle = long_bubble.locator(".atlas-message-toggle")
        toggle.wait_for()
        assert long_bubble.evaluate(
            "bubble => bubble.scrollWidth <= bubble.clientWidth"
        )
        assert short_bubble.evaluate(
            "bubble => bubble.scrollWidth <= bubble.clientWidth"
        )
        cards = long_bubble.locator(".mb-1 > .self-end > button")
        assert cards.count() == 3
        card_measurements = cards.evaluate_all(
            """cards => cards.map(card => {
              const bubble = card.closest('.user-message');
              const name = card.querySelector('.truncate');
              return {
                cardWidth: card.getBoundingClientRect().width,
                innerWidth: bubble.clientWidth - parseFloat(getComputedStyle(bubble).paddingLeft) - parseFloat(getComputedStyle(bubble).paddingRight),
                nameOverflows: name.scrollWidth > name.clientWidth,
                title: name.title,
                textOverflow: getComputedStyle(name).textOverflow,
                sizeVisible: card.querySelector('.text-gray-500').getBoundingClientRect().width > 0,
              };
            })"""
        )
        assert all(
            card["cardWidth"] <= card["innerWidth"] + 1
            and card["title"]
            and card["textOverflow"] == "ellipsis"
            and card["sizeVisible"]
            for card in card_measurements
        ), card_measurements
        if width == 390:
            assert all(card["nameOverflows"] for card in card_measurements), (
                card_measurements
            )
        collapsed = long_bubble.locator(".markdown-prose")
        collapsed_height = collapsed.evaluate(
            "node => node.getBoundingClientRect().height"
        )
        line_height = collapsed.evaluate(
            "node => parseFloat(getComputedStyle(node).lineHeight)"
        )
        assert collapsed_height <= line_height * 8 + 1, (collapsed_height, line_height)
        assert toggle.get_attribute("aria-expanded") == "false"
        long_bubble.screenshot(path=OUTPUT / name)
        toggle.focus()
        page.keyboard.press("Enter")
        assert toggle.get_attribute("aria-expanded") == "true"
        assert (
            collapsed.evaluate("node => node.getBoundingClientRect().height")
            > collapsed_height
        )
        assert "atlas-long-message" not in (short_bubble.get_attribute("class") or "")
        MEASUREMENTS[name] = {
            "style": style,
            "theme": theme,
            "size": size,
            "viewport_width": width,
            "cards": card_measurements,
            "collapsed_height": collapsed_height,
            "line_height": line_height,
            "toggle_expanded": True,
        }
    finally:
        page.close()


def capture(page: Page, name: str, style: str, theme: str, size: int = 16) -> None:
    page.evaluate(
        """([style, theme, size]) => {
            localStorage.setItem('atlas-ui', JSON.stringify({style, mode: theme, accent: 'green', size: String(size)}));
            localStorage.setItem('theme', theme);
        }""",
        [style, theme, size],
    )
    page.reload(wait_until="networkidle")
    page.locator(".chat-assistant").wait_for()
    page.evaluate("document.fonts.ready")
    page.locator("#chat-input").fill("Ask a follow-up")
    page.locator("#messages-container").evaluate("e => e.scrollTop = 0")
    MEASUREMENTS[name] = measure(page, style, size)
    page.screenshot(path=OUTPUT / name, full_page=False)


OUTPUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="fr-FR")
    page.goto(URL, wait_until="networkidle")
    try:
        page.get_by_text("Okay, Let's Go!", exact=True).click(timeout=1000)
    except Exception:
        pass
    if os.environ.get("ATLAS_COMPOSER_ONLY") == "1":
        for style in ("arrondi", "dense", "document"):
            for theme in ("light", "dark"):
                for size in (14, 20):
                    long_paste_capture(
                        browser,
                        style,
                        theme,
                        size,
                        screenshot=(
                            PASTE_SCREENSHOT_DIR
                            / f"ui-paste-long-{style}-{theme}-{size}.png"
                            if PASTE_SCREENSHOT_DIR is not None
                            else None
                        ),
                    )
        if os.environ.get("ATLAS_LONG_PASTE_ONLY") == "1":
            (OUTPUT / "measurements.json").write_text(
                json.dumps(MEASUREMENTS, indent=2) + "\n"
            )
            browser.close()
            raise SystemExit
        # The empty native chat is enough to verify queued attachments; do not
        # manufacture a conversation or call a model merely for layout evidence.
        if os.environ.get("ATLAS_COMPOSER_CAPTURES_ONLY") != "1":
            for style in ("arrondi", "dense", "document"):
                for theme in ("light", "dark"):
                    for size in (14, 16, 18, 20):
                        composer_capture(
                            browser,
                            f"composer-matrix-{style}-{theme}-{size}-1280",
                            style,
                            theme,
                            size,
                            1280,
                            synthetic_images(1),
                        )
                        composer_capture(
                            browser,
                            f"composer-matrix-{style}-{theme}-{size}-390",
                            style,
                            theme,
                            size,
                            390,
                            synthetic_images(1),
                        )
        composer_capture(
            browser,
            "composer-one-image.png",
            "arrondi",
            "light",
            16,
            1280,
            synthetic_images(1),
            True,
        )
        composer_capture(
            browser,
            "composer-three-images.png",
            "dense",
            "light",
            18,
            1280,
            synthetic_images(3),
            True,
        )
        composer_capture(
            browser,
            "composer-pdf-and-long-name.png",
            "document",
            "light",
            20,
            1280,
            [synthetic_pdf()],
            True,
        )
        composer_capture(
            browser,
            "composer-mobile.png",
            "arrondi",
            "light",
            16,
            390,
            synthetic_images(3),
            True,
        )
        composer_capture(
            browser,
            "composer-dark.png",
            "document",
            "dark",
            20,
            1280,
            synthetic_images(1),
            True,
        )
        (OUTPUT / "measurements.json").write_text(
            json.dumps(MEASUREMENTS, indent=2) + "\n"
        )
        browser.close()
        raise SystemExit
    if os.environ.get("ATLAS_BUBBLE_ONLY") == "1":
        for style, theme in (
            ("arrondi", "light"),
            ("arrondi", "dark"),
            ("dense", "light"),
        ):
            for size in (16, 20):
                for width in (1280, 390):
                    bubble_capture(browser, style, theme, size, width)
        (OUTPUT / "measurements.json").write_text(
            json.dumps(MEASUREMENTS, indent=2) + "\n"
        )
        browser.close()
        raise SystemExit
    page.locator(".chat-assistant").wait_for()
    assert page.locator(".user-message").count() == 1
    assert page.locator("#atlas-ui-selector").count() == 1
    page.evaluate(
        "localStorage.setItem('sidebar', 'true'); localStorage.setItem('locale', 'en-US')"
    )
    capture(page, "arrondi-light.png", "arrondi", "light")
    capture(page, "arrondi-dark.png", "arrondi", "dark")
    capture(page, "dense-light.png", "dense", "light")
    capture(page, "document-light.png", "document", "light")
    page.locator("#atlas-ui-toggle").click()
    popover_rows(page)
    assert page.locator("#atlas-ui-popover").is_visible()
    page.screenshot(path=OUTPUT / "popover.png", full_page=False)
    page.evaluate("localStorage.setItem('locale', 'fr-FR')")
    page.reload(wait_until="networkidle")
    page.locator("#atlas-ui-toggle").click()
    assert page.get_by_text("Paramètres d’affichage", exact=True).is_visible()
    page.screenshot(path=OUTPUT / "popover-fr.png", full_page=False)
    page.keyboard.press("Escape")
    assert page.locator("#atlas-ui-toggle").get_attribute("aria-expanded") == "false"
    assert page.locator("#atlas-ui-toggle").evaluate("e=>e===document.activeElement")
    for size in (14, 18, 20):
        capture(page, f"arrondi-light-{size}px.png", "arrondi", "light", size)
    # The interface remains English; browser language alone selects French.
    page.evaluate("localStorage.setItem('locale', 'en-US')")
    capture(page, "arrondi-light.png", "arrondi", "light")
    page.locator("#atlas-ui-toggle").click()
    assert page.get_by_text("Paramètres d’affichage", exact=True).is_visible()
    popover_rows(page)
    page.screenshot(path=OUTPUT / "popover-fr.png", full_page=False)
    for width in (360, 359):
        page.set_viewport_size({"width": width, "height": 900})
        if width == 360:
            popover_rows(page)
    page.set_viewport_size({"width": 1280, "height": 900})
    # An explicit appearance override has precedence over the browser locale.
    page.evaluate("localStorage.setItem('atlas-ui-language', 'de-DE')")
    page.reload(wait_until="networkidle")
    page.locator("#atlas-ui-toggle").click()
    assert page.get_by_text("Anzeigeeinstellungen", exact=True).is_visible()
    popover_rows(page)
    page.keyboard.press("Escape")
    MEASUREMENTS["settings_matrix"] = settings_matrix(page)
    MEASUREMENTS["languages"] = languages(browser, URL)
    resize_sidebar(page)
    MEASUREMENTS["native_sidebar_resize"] = True
    # The attachment composer is real Open WebUI state, covered at every style,
    # theme, owner text size and both requested viewport widths.
    for style in ("arrondi", "dense", "document"):
        for theme in ("light", "dark"):
            for size in (14, 20):
                long_paste_capture(
                    browser,
                    style,
                    theme,
                    size,
                    screenshot=(
                        PASTE_SCREENSHOT_DIR
                        / f"ui-paste-long-{style}-{theme}-{size}.png"
                        if PASTE_SCREENSHOT_DIR is not None
                        else None
                    ),
                )
                composer_capture(
                    browser,
                    f"composer-matrix-{style}-{theme}-{size}-{1280}.png",
                    style,
                    theme,
                    size,
                    1280,
                    synthetic_images(1),
                )
                composer_capture(
                    browser,
                    f"composer-matrix-{style}-{theme}-{size}-{390}.png",
                    style,
                    theme,
                    size,
                    390,
                    synthetic_images(1),
                )
    composer_capture(
        browser,
        "composer-one-image.png",
        "arrondi",
        "light",
        16,
        1280,
        synthetic_images(1),
        True,
    )
    composer_capture(
        browser,
        "composer-three-images.png",
        "dense",
        "light",
        18,
        1280,
        synthetic_images(3),
        True,
    )
    composer_capture(
        browser,
        "composer-pdf-and-long-name.png",
        "document",
        "light",
        20,
        1280,
        [synthetic_pdf()],
        True,
    )
    composer_capture(
        browser,
        "composer-mobile.png",
        "arrondi",
        "light",
        16,
        390,
        synthetic_images(3),
        True,
    )
    composer_capture(
        browser,
        "composer-dark.png",
        "document",
        "dark",
        20,
        1280,
        synthetic_images(1),
        True,
    )
    (OUTPUT / "measurements.json").write_text(json.dumps(MEASUREMENTS, indent=2) + "\n")
    browser.close()
