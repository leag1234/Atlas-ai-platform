"""Open one app file in headless Chromium and report what a person would hit.

Runs on its own (standard library and Playwright only), inside the render
container or, for development, on the host: services/orchestrator/app_render.py
passes this file's source to ``python -I -c``, the settings as the first
argument (JSON) and the HTML file on standard input. It prints one JSON object
per line: {"stage": "launched"}, {"stage": "loaded"}, then {"result": {...}}.

The page is served at a fixed https address by request interception with the
side view's headers (CSP with sandbox, Permissions-Policy); every other request
is aborted and recorded. Nothing reaches the network.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

APP_URL = "https://app.atlas-render.invalid/app.html"
BUTTONS = "button, [role=button], input[type=button], input[type=submit]"
MAX_ITEMS = 8
MAX_TEXT = 160

# The page records its own policy violations, listeners and sound starts.
# A sound started before the person's first press or key is blocked by the
# browser's autoplay rule ("The AudioContext was not allowed to start"): the
# check's own evaluate() calls count as a gesture for the browser, so the page
# notes trusted presses itself.
INIT_SCRIPT = """
(() => {
  const s = (window.__atlasRender = { violations: [], listeners: {}, gesture: false, audio: [] });
  // A fixed sequence for Math.random: the same file shows the same states
  // (questions, levels) at each check, so a repair is judged on the same run.
  let seed = 0x2f6b4c1d;
  Math.random = () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  for (const type of ['pointerdown', 'mousedown', 'keydown', 'touchstart', 'click']) {
    window.addEventListener(type, (e) => { if (e.isTrusted) s.gesture = true; }, true);
  }
  const early = (what) => {
    if (!s.gesture && s.audio.length < 4 && !s.audio.includes(what)) s.audio.push(what);
  };
  for (const name of ['AudioContext', 'webkitAudioContext']) {
    const Original = window[name];
    if (typeof Original !== 'function') continue;
    const Wrapped = function (...args) {
      early(name);
      return new Original(...args);
    };
    Wrapped.prototype = Original.prototype;
    try { Object.defineProperty(window, name, { value: Wrapped, configurable: true, writable: true }); } catch (e) {}
  }
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    if (!this.muted) early(this.tagName.toLowerCase() + '.play()');
    return play.apply(this, args);
  };
  document.addEventListener('securitypolicyviolation', (e) => {
    s.violations.push({
      directive: String(e.effectiveDirective || e.violatedDirective || ''),
      uri: String(e.blockedURI || '').slice(0, 120),
    });
  }, true);
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, options) {
    try { s.listeners[type] = (s.listeners[type] || 0) + 1; } catch (e) {}
    return add.call(this, type, fn, options);
  };
})();
"""

INSPECT = """
() => {
  const s = window.__atlasRender || { violations: [], listeners: {} };
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom <= 0 || r.right <= 0) return false;
    if (r.left >= innerWidth) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.05;
  };
  const body = document.body;
  let visible = false;
  if (body) {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    while (!visible && walker.nextNode()) {
      const node = walker.currentNode;
      visible = !!(node.textContent.trim() && node.parentElement && shown(node.parentElement));
    }
    const all = body.querySelectorAll('*');
    for (let i = 0; !visible && i < all.length && i < 5000; i++) {
      const el = all[i];
      const tag = el.tagName.toLowerCase();
      if (['canvas', 'svg', 'img', 'video', 'input', 'select', 'textarea', 'button', 'progress', 'meter'].includes(tag)) {
        visible = shown(el);
        continue;
      }
      const st = getComputedStyle(el);
      const painted = (st.backgroundColor !== 'rgba(0, 0, 0, 0)' && st.backgroundColor !== 'transparent')
        || st.backgroundImage !== 'none' || parseFloat(st.borderTopWidth) > 0;
      if (painted) {
        const r = el.getBoundingClientRect();
        visible = r.width >= 16 && r.height >= 16 && shown(el);
      }
    }
  }
  const events = ['click', 'pointerdown', 'pointerup', 'mousedown', 'mouseup', 'touchstart',
    'touchend', 'submit', 'change', 'input', 'keydown', 'keyup', 'keypress'];
  let handlers = events.some((t) => s.listeners[t] > 0);
  const props = events.map((t) => 'on' + t);
  if (!handlers) {
    const targets = [window, document, ...(body ? body.querySelectorAll('*') : [])];
    handlers = targets.some((el) => props.some((p) => typeof el[p] === 'function'));
  }
  const buttons = body ? [...body.querySelectorAll(%s)].filter(shown).length : 0;
  return { visible, buttons, handlers, violations: s.violations.slice(0, 20) };
}
""" % json.dumps(BUTTONS)

# Every <img> of the page, shown or not: one without a source, or whose source
# did not decode, is a broken picture in the side view.
IMAGES = """
() => [...document.images].map((img) => {
  const src = (img.getAttribute('src') || '').trim();
  const srcset = (img.getAttribute('srcset') || '').trim();
  const name = img.id ? '#' + img.id : (img.alt ? 'alt="' + img.alt.slice(0, 40) + '"' : 'img');
  if (!src && !srcset) return { name, src: '', reason: 'empty' };
  if (img.complete && img.naturalWidth === 0) return { name, src: src.slice(0, 60), reason: 'unloadable' };
  return null;
}).filter(Boolean).slice(0, 8)
"""
AUDIO = "() => ((window.__atlasRender || {}).audio || []).slice(0, 4)"


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def short(text: object) -> str:
    return " ".join(str(text).split())[:MAX_TEXT]


def main() -> int:
    settings: dict[str, Any] = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    document = sys.stdin.buffer.read()
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": str(settings.get("csp", "")),
        "Permissions-Policy": str(settings.get("permissions", "")),
        "Cache-Control": "no-store",
    }
    load_ms = int(settings.get("load_ms", 6000))
    settle_ms = int(settings.get("settle_ms", 800))
    clicks = int(settings.get("clicks", 3))
    # Timers run forward on the page's clock (a countdown, a turn timer, a
    # sound at the end of a question): what happens after a while with nobody
    # touching the page is checked too, in little real time.
    later_ms = int(settings.get("later_ms", 0))
    later_budget_ms = int(settings.get("later_budget_ms", 2500))
    from playwright.sync_api import ConsoleMessage, Error, Route, sync_playwright

    errors: list[dict[str, str]] = []
    blocked: list[str] = []
    refused: list[str] = []
    phase = {"name": "load"}
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--mute-audio",
                "--js-flags=--max-old-space-size=192",
            ],
        )
        emit({"stage": "launched"})
        page = browser.new_page(viewport={"width": 480, "height": 720})
        page.set_default_timeout(2000)
        page.add_init_script(INIT_SCRIPT)
        if later_ms > 0:
            page.clock.install()
        page.on(
            "pageerror",
            lambda error: errors.append(
                {"phase": phase["name"], "message": short(error)}
            ),
        )

        def console(message: ConsoleMessage) -> None:
            text = short(message.text)
            if message.type == "error" and text.startswith("Refused"):
                refused.append(text)

        page.on("console", console)
        page.on("dialog", lambda dialog: dialog.dismiss())

        def route(request_route: Route) -> None:
            url = request_route.request.url
            if url == APP_URL:
                request_route.fulfill(status=200, headers=headers, body=document)
                return
            blocked.append(short(url)[:120])
            request_route.abort()

        page.route("**/*", route)
        loaded = True
        try:
            page.goto(APP_URL, wait_until="load", timeout=load_ms)
        except Error as error:
            loaded = False
            errors.append({"phase": "load", "message": "not_loaded: " + short(error)})
        emit({"stage": "loaded"})
        page.wait_for_timeout(settle_ms)
        state: dict[str, Any] = page.evaluate(INSPECT) if loaded else {}
        images: list[dict[str, str]] = []

        def note_images() -> None:
            for image in page.evaluate(IMAGES):
                if isinstance(image, dict) and image not in images:
                    images.append(image)

        def run_later(step: int) -> None:
            try:
                page.clock.run_for(step)
            except Error as error:
                # A timer callback that throws surfaces here, not as a
                # pageerror: the fake clock calls it from evaluate().
                first = str(error).split("\n", 1)[0]
                message = first.removeprefix("Clock.run_for: ")
                entry = {"phase": "later", "message": short(message)}
                if entry not in errors and len(errors) < MAX_ITEMS:
                    errors.append(entry)

        if loaded:
            note_images()
        waited = 0
        if loaded and later_ms > 0:
            phase["name"] = "later"
            spent = time.monotonic()
            while (
                waited < later_ms
                and (time.monotonic() - spent) * 1000 < later_budget_ms
            ):
                step = min(1000, later_ms - waited)
                run_later(step)
                waited += step
            note_images()
        clicked: list[str] = []
        if loaded and clicks > 0:
            phase["name"] = "click"
            for handle in page.query_selector_all(BUTTONS)[: clicks * 3]:
                if len(clicked) >= clicks:
                    break
                try:
                    if not handle.is_visible() or not handle.is_enabled():
                        continue
                    label = short(handle.inner_text() or handle.get_attribute("value"))
                    before = len(errors)
                    handle.click(timeout=1000, no_wait_after=True)
                    page.wait_for_timeout(200)
                    clicked.append(label)
                    for entry in errors[before:]:
                        entry["control"] = label[:40]
                except Error:
                    continue
            if clicked:
                if later_ms > 0:
                    # What the presses scheduled (the next question) is shown.
                    run_later(2000)
                final = page.evaluate("() => (window.__atlasRender||{}).violations||[]")
                state["violations"] = final[:20]
                note_images()
        audio = list(page.evaluate(AUDIO)) if loaded else []
        browser.close()
    emit(
        {
            "result": {
                "loaded": loaded,
                "visible": bool(state.get("visible")),
                "buttons": int(state.get("buttons") or 0),
                "handlers": bool(state.get("handlers")),
                "errors": errors[:MAX_ITEMS],
                "violations": list(state.get("violations") or [])[:MAX_ITEMS],
                "refused": refused[:MAX_ITEMS],
                "blocked": blocked[:MAX_ITEMS],
                "clicked": clicked,
                "images": images[:MAX_ITEMS],
                "audio": audio,
                "waited_ms": waited,
                "ms": int((time.monotonic() - started) * 1000),
            }
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
