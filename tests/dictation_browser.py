"""Chat voice button end to end: real Open WebUI shell, Atlas UI relay, dictation relay.

Headless Chromium plays a synthetic WAV as its microphone (fake media device). The stack
under test is started separately and must not be a shared instance:

- an Open WebUI container (the pinned image) with the worktree's chat-ui.js and chat-ui.css,
  WEBUI_AUTH=False, its model list served by a local test stub;
- the Atlas adapter (services.orchestrator.chat_api) with ATLAS_REALTIME_DICTATION=1;
- the Atlas UI relay (terminal_stack.loopback_html_injector) in front of both.

Checks: the Atlas button sits exactly over the native microphone (hidden, still in the
layout) in light and dark, English and French, desktop and phone widths; live text appears
in the floating panel; clicking outside keeps it; the final text lands in the composer once,
at the cursor, and Open WebUI's own state sees it (the send button appears and sending sends
it); the composer subtree gets no mutation while the user speaks; no page error; a refused
microphone gives a readable sentence.

Usage: python tests/dictation_browser.py BASE_URL AUDIO_DIR OUT_DIR [SENT_LOG]
(AUDIO_DIR holds fr.wav and en.wav; SENT_LOG is the stub's record of sent messages.)
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import Browser, Page, Playwright, ViewportSize, sync_playwright

BASE, AUDIO, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2]), Path(sys.argv[3])
SENT = Path(sys.argv[4]) if len(sys.argv) > 4 else None
OUT.mkdir(parents=True, exist_ok=True)
DESKTOP: ViewportSize = {"width": 1440, "height": 900}
PHONE: ViewportSize = {"width": 390, "height": 844}
# Per interface language: browser locale, button label, two words of the spoken sentence
# (AUDIO_DIR/<code>.wav) and the local engine status line.
LANGUAGES = {
    "fr": (
        "fr-FR",
        "Dicter",
        ("table", "personnes"),
        "Transcrit sur les serveurs Atlas",
    ),
    "en": ("en-US", "Dictate", ("quarterly", "Friday"), "Transcribed on Atlas servers"),
    "de": (
        "de-DE",
        "Diktieren",
        ("Tisch", "Personen"),
        "Auf Atlas-Servern transkribiert",
    ),
    "es": (
        "es-ES",
        "Dictar",
        ("mesa", "personas"),
        "Transcrito en los servidores de Atlas",
    ),
    "it": ("it-IT", "Detta", ("tavolo", "persone"), "Trascritto sui server di Atlas"),
    "nl": (
        "nl-NL",
        "Dicteren",
        ("tafel", "personen"),
        "Getranscribeerd op Atlas-servers",
    ),
}
EXPECTED = {code: value[2] for code, value in LANGUAGES.items()}
REFUSED = {
    "fr": "L’accès au micro a été refusé. Autorisez-le dans les réglages du site du navigateur, puis réessayez.",
    "en": "Microphone access was refused. Allow it in the browser's site settings, then try again.",
}
NO_MIC = {
    "fr": "Aucun micro n’a été trouvé sur cet appareil.",
    "en": "No microphone was found on this device.",
}
results: dict[str, Any] = {}
failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    results.setdefault("checks", []).append([name, bool(condition), detail])
    if not condition:
        failures.append(f"{name}: {detail}")


def launch(play: Playwright, wav: Path | None, *, device: bool = True) -> Browser:
    args = ["--autoplay-policy=no-user-gesture-required"]
    if device:
        args.append("--use-fake-device-for-media-stream")
    if wav is not None:
        args += [
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}%noloop",
        ]
    return play.chromium.launch(args=args)


def open_chat(
    browser: Browser, lang: str, theme: str, viewport: ViewportSize, mic: bool = True
) -> tuple[Page, list[str]]:
    phone = viewport["width"] < 768
    context = browser.new_context(
        viewport=viewport,
        locale=LANGUAGES[lang][0],
        permissions=["microphone"] if mic else [],
        is_mobile=phone,
        has_touch=phone,
        device_scale_factor=2 if phone else 1,
    )
    context.add_init_script(
        f"localStorage.setItem('locale', '{lang}'); localStorage.setItem('theme', '{theme}');"
    )
    page = context.new_page()
    errors: list[str] = []
    page.on(
        "pageerror",
        lambda error: errors.append(
            error.message + " @ " + " | ".join((error.stack or "").splitlines()[1:3])
        ),
    )
    page.goto(BASE + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("#chat-input", timeout=60000)
    except PlaywrightTimeout:
        # A busy shared host can stall the first load of the interface: one reload.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#chat-input", timeout=90000)
    page.wait_for_timeout(1500)
    # A fresh account first sees the release notes dialog: the button stays hidden under it.
    if page.locator(".modal").count():
        hidden = page.evaluate(
            "() => document.querySelector('.atlas-dictation-button')?.hidden ?? true"
        )
        check("button hidden under a dialog", hidden)
        page.locator(".modal button").last.click()
        page.wait_for_selector(".modal", state="detached", timeout=10000)
    page.wait_for_selector(".atlas-dictation-button:not([hidden])", timeout=30000)
    page.wait_for_timeout(800)
    return page, errors


def placement(page: Page) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """() => {
              const ours = document.querySelector('.atlas-dictation-button').getBoundingClientRect();
              const native = document.querySelector('#voice-input-button');
              const rect = native.getBoundingClientRect();
              return { ours: [ours.x, ours.y, ours.width, ours.height], native: [rect.x, rect.y, rect.width, rect.height],
                       visibility: getComputedStyle(native).visibility, inBody: document.querySelector('.atlas-dictation-button').parentElement === document.body,
                       dark: document.documentElement.classList.contains('dark'), label: document.querySelector('.atlas-dictation-button').getAttribute('aria-label') };
            }"""
        )
    )


def composer_shot(page: Page, name: str, viewport: ViewportSize) -> None:
    if viewport["width"] < 768:
        page.screenshot(path=str(OUT / name), caret="initial")
        return
    box = page.locator("#message-input-container").bounding_box()
    assert box is not None
    top = max(0, box["y"] - 260)
    page.screenshot(
        path=str(OUT / name),
        caret="initial",
        clip={
            "x": max(0, box["x"] - 40),
            "y": top,
            "width": min(box["width"] + 80, viewport["width"]),
            "height": min(box["y"] + box["height"] + 40, viewport["height"]) - top,
        },
    )


def placements() -> None:
    with sync_playwright() as play:
        browser = launch(play, AUDIO / "fr.wav")
        for lang in LANGUAGES:
            for theme in ("light", "dark"):
                for label, viewport in (("desktop", DESKTOP), ("phone", PHONE)):
                    page, errors = open_chat(browser, lang, theme, viewport)
                    where = placement(page)
                    name = f"button-{lang}-{theme}-{label}"
                    offset = max(
                        abs(a - b) for a, b in zip(where["ours"], where["native"])
                    )
                    check(f"{name} over the native microphone", offset <= 1, where)
                    check(
                        f"{name} native microphone hidden",
                        where["visibility"] == "hidden",
                        where["visibility"],
                    )
                    check(f"{name} button in document.body", where["inBody"])
                    check(f"{name} theme applied", where["dark"] == (theme == "dark"))
                    check(
                        f"{name} label",
                        where["label"] == LANGUAGES[lang][1],
                        where["label"],
                    )
                    check(f"{name} no page error", not errors, errors)
                    composer_shot(page, name + ".png", viewport)
                    page.context.close()
        browser.close()


WATCH = """() => {
  // Test-only observer: counts composer mutations while dictating, until the single insertion.
  // A click outside the panel is the user's own action: ProseMirror then marks its blur, which
  // is counted apart.
  const target = document.querySelector('#message-input-container');
  window.__mutations = { count: 0, outside: 0, after: 0, inserts: 0, seen: [] };
  const describe = node => node.nodeType === 1 ? node.tagName.toLowerCase() + (node.id ? '#' + node.id : '') + '.' + String(node.className || '').slice(0, 40) : '#text';
  new MutationObserver(records => {
    const state = window.__mutations;
    for (const record of records) {
      if (state.inserts) state.after += 1; else if (window.__phase === 'click-outside') state.outside += 1; else state.count += 1;
      if (state.seen.length < 40) state.seen.push([state.inserts ? 'insert' : window.__phase || '', record.type, describe(record.target), record.attributeName || '']);
    }
  }).observe(target, { subtree: true, childList: true, characterData: true, attributes: true });
  window.addEventListener('atlas:dictation-insert', () => { window.__mutations.inserts += 1; });
}"""


STATUS = {
    "local": {code: value[3] for code, value in LANGUAGES.items()},
    "fallback": {
        "fr": "Moteur Atlas occupé : transcrit par un service externe",
        "en": "Atlas engine busy: transcribed by an external service",
    },
}


def dictate(
    lang: str,
    theme: str,
    viewport: ViewportSize,
    *,
    stop: str,
    prefix: str,
    auto: bool,
    engine: str = "local",
    suffix: str = "",
) -> None:
    name = (
        f"flow-{lang}-{theme}-{'phone' if viewport['width'] < 768 else 'desktop'}-{stop}"
        + ("" if engine == "local" else "-" + engine)
    )
    with sync_playwright() as play:
        browser = launch(play, AUDIO / f"{lang}.wav")
        page, errors = open_chat(browser, lang, theme, viewport)
        if auto:
            page.evaluate("localStorage.setItem('atlas-dictation-auto-insert', '1')")
        page.click("#chat-input")
        page.keyboard.type(prefix + suffix)
        # The cursor goes back before the suffix: the dictated text must land there.
        for _ in suffix:
            page.keyboard.press("ArrowLeft")
        # The pointer leaves the composer and its tooltips settle before the count starts.
        page.mouse.move(viewport["width"] // 2, 60)
        page.wait_for_timeout(1200)
        sent_before = (
            len(SENT.read_text().splitlines()) if SENT and SENT.exists() else 0
        )
        page.evaluate(WATCH)
        started = time.monotonic()
        page.evaluate("window.__phase = 'start'")
        page.click(".atlas-dictation-button")
        page.wait_for_selector(".atlas-dictation-panel:not([hidden])", timeout=10000)
        page.wait_for_function(
            "() => (document.querySelector('.atlas-dictation-text')?.innerText || '').trim().length > 0",
            timeout=30000,
        )
        first_text = round(time.monotonic() - started, 2)
        page.evaluate("window.__phase = 'speaking'")
        page.wait_for_timeout(700)
        composer_shot(page, name + "-1-live.png", viewport)
        live = page.evaluate(
            "() => ({ stable: document.querySelector('.atlas-dictation-stable').textContent, open: document.querySelector('.atlas-dictation-open').textContent, status: document.querySelector('.atlas-dictation-status').textContent })"
        )
        check(
            f"{name} engine status",
            STATUS[engine][lang] in live["status"],
            live["status"],
        )
        # A click outside the panel keeps the dictation and its text.
        page.evaluate("window.__phase = 'click-outside'")
        page.mouse.click(viewport["width"] // 2, 120)
        page.wait_for_timeout(300)
        page.evaluate("window.__phase = 'speaking'")
        kept = page.evaluate(
            "() => !document.querySelector('.atlas-dictation-panel').hidden && document.querySelector('.atlas-dictation-text').innerText.trim().length > 0"
        )
        check(f"{name} click outside keeps the text", kept)
        if not auto:
            page.wait_for_function(
                "words => words.every(word => document.querySelector('.atlas-dictation-text').innerText.toLowerCase().includes(word.toLowerCase()))",
                arg=list(EXPECTED[lang]),
                timeout=40000,
            )
            page.wait_for_timeout(600)
            composer_shot(page, name + "-2-stable.png", viewport)
            during = page.evaluate("() => window.__mutations")
            check(
                f"{name} no composer mutation while speaking",
                during["count"] == 0,
                during,
            )
            if stop == "esc":
                page.keyboard.press("Escape")
            else:
                page.click(".atlas-dictation-stop")
        page.wait_for_selector(".atlas-dictation-panel", state="hidden", timeout=40000)
        inserted = round(time.monotonic() - started, 2)
        page.wait_for_timeout(500)
        mutations = page.evaluate("() => window.__mutations")
        text = page.evaluate("() => document.querySelector('#chat-input').innerText")
        composer_shot(page, name + "-3-inserted.png", viewport)
        check(
            f"{name} zero composer mutations before the single insertion",
            mutations["count"] == 0,
            mutations,
        )
        check(f"{name} one insertion", mutations["inserts"] == 1, mutations)
        check(
            f"{name} text inserted at the cursor",
            text.startswith(prefix.strip())
            and text.rstrip().endswith(suffix.strip())
            and text.count(prefix.strip() or text) == 1,
            text,
        )
        check(
            f"{name} dictated words in the composer",
            all(word.lower() in text.lower() for word in EXPECTED[lang]),
            text,
        )
        check(
            f"{name} words not doubled",
            text.lower().count(EXPECTED[lang][0].lower()) == 1,
            text,
        )
        send = page.locator("#send-message-button")
        check(
            f"{name} send button active (Open WebUI state updated)",
            send.count() == 1 and send.is_visible() and send.is_enabled(),
        )
        send.click()
        page.wait_for_function(
            "word => [...document.querySelectorAll('.user-message, .chat-user')].some(node => node.innerText.includes(word))",
            arg=EXPECTED[lang][0],
            timeout=20000,
        )
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / (name + "-4-sent.png")), caret="initial")
        if SENT is not None:
            lines = SENT.read_text().splitlines()[sent_before:]
            sent = json.loads(lines[-1])["text"] if lines else ""
            check(
                f"{name} sent text reached the model",
                all(w.lower() in sent.lower() for w in EXPECTED[lang]),
                sent,
            )
        check(f"{name} no page error", not errors, errors)
        results[name] = {
            "first_text_s": first_text,
            "inserted_s": inserted,
            "live": live,
            "composer": text,
            "mutations": mutations,
        }
        browser.close()


def refused(lang: str, *, device: bool) -> None:
    """Permission refused (a microphone exists, the site may not use it) or no microphone."""
    name = f"{'refused' if device else 'no-microphone'}-{lang}"
    with sync_playwright() as play:
        browser = launch(play, None, device=device)
        page, errors = open_chat(
            browser, lang, "dark" if device else "light", DESKTOP, mic=False
        )
        if device:
            # Headless Chromium has no permission prompt to refuse (it answers NotSupportedError):
            # the page gets the browser's refusal as a real one does.
            page.evaluate(
                "() => { navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError')); }"
            )
        page.click(".atlas-dictation-button")
        page.wait_for_selector(".atlas-dictation-message:not([hidden])", timeout=15000)
        message = page.locator(".atlas-dictation-message").inner_text()
        composer_shot(page, name + ".png", DESKTOP)
        expected = (REFUSED if device else NO_MIC)[lang]
        check(f"{name} readable sentence", message == expected, message)
        page.keyboard.press("Escape")
        page.wait_for_selector(".atlas-dictation-panel", state="hidden", timeout=5000)
        check(f"{name} no page error", not errors, errors)
        browser.close()


ONLY = os.environ.get("DICTATION_BROWSER_ONLY", "")
if ONLY in ("", "placements"):
    placements()
if ONLY in ("", "flows"):
    dictate("fr", "light", DESKTOP, stop="button", prefix="Note : ", auto=False)
    dictate(
        "en",
        "dark",
        DESKTOP,
        stop="esc",
        prefix="Reminder: ",
        auto=False,
        suffix=" Thanks.",
    )
    dictate("en", "light", PHONE, stop="auto", prefix="Hi ", auto=True)
    for code, theme in (
        ("de", "dark"),
        ("es", "light"),
        ("it", "dark"),
        ("nl", "light"),
    ):
        dictate(code, theme, DESKTOP, stop="button", prefix="", auto=False)
if ONLY == "cloud":
    # Run against a relay whose local engine cap is 0 (ATLAS_DICTATION_LOCAL_MAX_SESSIONS=0).
    dictate(
        "fr", "light", DESKTOP, stop="button", prefix="", auto=False, engine="fallback"
    )
if ONLY in ("", "errors"):
    for code in ("fr", "en"):
        refused(code, device=True)
        refused(code, device=False)
results["failures"] = failures
(OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
print(
    json.dumps(
        {k: v for k, v in results.items() if k != "checks"},
        ensure_ascii=False,
        indent=1,
    )
)
print(
    f"{sum(1 for c in results['checks'] if c[1])}/{len(results['checks'])} checks passed"
)
sys.exit(1 if failures else 0)
