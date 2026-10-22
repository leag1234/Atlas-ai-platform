"""Numeric checks against the owner mockups on the saved-chat Open WebUI DOM."""

from typing import Any

from playwright.sync_api import Browser, Page


MEASURE = """() => {
  const rect = e => e.getBoundingClientRect();
  const css = e => getComputedStyle(e);
  const pick = s => document.querySelector(s);
  const bubble = pick('.user-message'), text = pick('.user-message .markdown-prose');
  const files = pick('.user-message .chat-user > .mb-1');
  const column = bubble.closest('.message-listitem');
  const side = pick('#sidebar'), panel = pick('#chat-container');
  const composer = pick('#message-input-container');
  const range = document.createRange(); range.selectNodeContents(text.querySelector('p'));
  const lines = new Set([...range.getClientRects()].filter(r=>r.width>0).map(r=>Math.round(r.top))).size;
  const fonts = Object.fromEntries(Object.entries({
    user:'.user-message p', answer:'.chat-assistant p', list:'.chat-assistant li',
    heading:'.chat-assistant h2', table:'.chat-assistant td', code:'.cm-line',
    ui:'#atlas-ui-toggle'
  }).map(([name,selector]) => {const c=css(pick(selector)); return [name,{size:parseFloat(c.fontSize),line:parseFloat(c.lineHeight)}]}));
  return {fonts, bubble:{height:rect(bubble).height, textHeight:rect(text).height,
    fileHeight:files ? rect(files).height : 0, lines, width:rect(bubble).width,
    columnWidth:rect(column).width, right:rect(column).right-rect(bubble).right,
    padding:[css(bubble).paddingTop,css(bubble).paddingRight,css(bubble).paddingBottom,css(bubble).paddingLeft].map(parseFloat),
    radius:parseFloat(css(bubble).borderRadius), color:css(text.querySelector('p')).color,
    expectedColor:css(bubble).color}, composer:{height:rect(composer).height,radius:parseFloat(css(composer).borderRadius)},
    sidebarWidth:rect(side).width, panelRadius:parseFloat(css(panel).borderRadius),
    gap:rect(panel).left-rect(side).right, frame:css(side.parentElement).backgroundColor,
    panelColor:css(panel).backgroundColor, sidebarColor:css(side).backgroundColor,
    sidebarRowHeight:rect(pick('#sidebar a[href^="/c/"]')).height,
    viewport:{width:innerWidth,height:innerHeight}, overflow:document.documentElement.scrollWidth>innerWidth};
}"""


def measure(page: Page, style: str, size: int) -> dict[str, Any]:
    """Assert geometry and actual utility-overridden text nodes, at every size."""
    values: dict[str, Any] = page.evaluate(MEASURE)
    for name in ("user", "answer", "list", "table", "code"):
        assert abs(values["fonts"][name]["size"] - size) < 0.1, (name, values)
        leading = 1.5 if name == "user" else 1.6
        assert abs(values["fonts"][name]["line"] - size * leading) < 0.1, (
            name,
            values,
        )
    assert abs(values["fonts"]["heading"]["size"] - size * 1.375) < 0.1, values
    assert values["fonts"]["ui"]["size"] == int(size * 0.85 + 0.5), values
    bubble = values["bubble"]
    assert bubble["padding"] == [10, 16, 10, 16], values
    assert bubble["radius"] == (20 if style == "arrondi" else 8), values
    assert bubble["color"] == bubble["expectedColor"], values
    assert abs(bubble["textHeight"] - bubble["lines"] * size * 1.5) <= 2, values
    expected = bubble["fileHeight"] + bubble["lines"] * size * 1.5 + 20
    assert abs(bubble["height"] - expected) <= 4, values
    assert bubble["width"] <= bubble["columnWidth"] * 0.8 + 1, values
    assert abs(bubble["right"]) <= 2, values
    expected_geometry = {
        "arrondi": (52, 26, 250, 20, 14, 34),
        "dense": (48, 10, 232, 0, 0, 30),
        "document": (48, 10, 230, 0, 0, 34),
    }[style]
    actual = (
        values["composer"]["height"],
        values["composer"]["radius"],
        values["sidebarWidth"],
        values["panelRadius"],
        values["gap"],
        values["sidebarRowHeight"],
    )
    for got, wanted in zip(actual, expected_geometry):
        assert abs(got - wanted) <= 2, (actual, expected_geometry, values)
    assert not values["overflow"], values
    return values


def popover_rows(page: Page) -> None:
    """Style and size stay on one row, even with larger localized text."""
    for group, count in (("style", 3), ("size", 4)):
        buttons = page.locator(f'[data-atlas-option="{group}"]')
        assert buttons.count() == count
        bounds = buttons.evaluate_all(
            "es=>es.map(e=>({top:e.getBoundingClientRect().top,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}))"
        )
        assert max(x["top"] for x in bounds) - min(x["top"] for x in bounds) <= 1
        assert all(x["width"] >= 44 and x["height"] >= 44 for x in bounds)
    assert page.locator("#atlas-ui-popover").evaluate(
        "e=>e.scrollWidth <= e.clientWidth && e.getBoundingClientRect().left >= 0"
    )


def settings_matrix(page: Page) -> dict[str, Any]:
    """Exercise actual controls for all styles, modes and sizes, then reload."""
    result: dict[str, Any] = {}
    for theme in ("light", "dark"):
        page.evaluate(
            "theme=>{localStorage.setItem('theme',theme); localStorage.setItem('sidebar','true')}",
            theme,
        )
        page.reload(wait_until="networkidle")
        for style in ("arrondi", "dense", "document"):
            for size in (14, 16, 18, 20):
                page.locator("#atlas-ui-toggle").click()
                page.locator(
                    f'[data-atlas-option="style"][data-value="{style}"]'
                ).click()
                page.locator(f'[data-atlas-option="size"][data-value="{size}"]').click()
                page.keyboard.press("Escape")
                page.evaluate("document.fonts.ready")
                page.locator("#messages-container").evaluate("e=>e.scrollTop=0")
                result[f"{style}-{theme}-{size}"] = measure(page, style, size)
        page.reload(wait_until="networkidle")
        assert page.locator("html").get_attribute("data-atlas-size") == "20"
        assert page.locator("html").get_attribute("data-atlas-style") == "document"
    return result


def languages(browser: Browser, url: str) -> dict[str, str]:
    """The selector follows browser language independently of the interface."""
    result = {}
    for locale, label in (
        ("fr-FR", "Paramètres d’affichage"),
        ("en-US", "Display settings"),
        ("de-DE", "Anzeigeeinstellungen"),
        ("es-ES", "Ajustes de pantalla"),
        ("it-IT", "Impostazioni di visualizzazione"),
        ("ja-JP", "Display settings"),
    ):
        context = browser.new_context(locale=locale)
        context.add_init_script("localStorage.setItem('locale','en-US')")
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        assert page.locator("#atlas-ui-toggle").get_attribute("aria-label") == label
        result[locale] = label
        context.close()
    return result


def resize_sidebar(page: Page) -> None:
    """Keep the native drag handle functional while matching the default width."""
    page.evaluate(
        "localStorage.removeItem('sidebarWidth'); localStorage.setItem('sidebar','true')"
    )
    page.reload(wait_until="networkidle")
    before = page.locator("#sidebar").bounding_box()
    handle = page.locator("#sidebar-resizer").bounding_box()
    assert before is not None and handle is not None
    page.mouse.move(handle["x"], 400)
    page.mouse.down()
    page.mouse.move(handle["x"] + 40, 400, steps=5)
    page.mouse.up()
    page.wait_for_timeout(300)
    after = page.locator("#sidebar").bounding_box()
    assert after is not None
    assert abs(after["width"] - before["width"] - 40) <= 2
