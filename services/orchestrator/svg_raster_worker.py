"""Rasterise SVG files to PNG in headless Chromium, with no network.

Runs on its own (standard library and Playwright only), inside the render
container (infra/docker/Dockerfile.app-render, ``--network none``) or, for
development, on the host: services/orchestrator/svg_raster.py passes this
file's source to ``python -I -c``, the settings as the first argument (JSON)
and the files on standard input as JSON ``{"items": [<base64 SVG>, ...]}``. It
prints one JSON object per line: {"stage": "launched"}, then one
{"item": i, "png": <base64>, "width": w, "height": h, "left_out": n} or
{"item": i, "error": <code>} per file, then {"done": {"blocked": [...]}}
(``left_out`` counts the references to other documents taken out).

Each SVG is first parsed with DOMParser (an inert document: nothing in it runs
or loads) to check it is an SVG document, count its elements and read its size;
then it is drawn as an image (``<img src="data:image/svg+xml...">``), the mode
where browsers run no script and load no external resource, and the drawn
element is captured as a PNG. The page is served at a fixed https address by
request interception with a policy that allows data: images only; every other
request is aborted and recorded. Only pixels leave the container.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

HOST_URL = "https://svg.atlas-render.invalid/host.html"
HOST_PAGE = (
    b"<!DOCTYPE html><html><head><meta charset='utf-8'>"
    b"<style>html,body{margin:0;padding:0;background:transparent}"
    b"img{display:block}</style></head><body></body></html>"
)
HOST_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "script-src 'none'; base-uri 'none'; form-action 'none'"
)

# Parse, check and size one SVG; returns {width, height, svg} or {error}.
PREPARE = """
([source, maxSide, minSide, maxElements]) => {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  let text;
  try {
    const bytes = Uint8Array.from(atob(source), (c) => c.charCodeAt(0));
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch (e) { return { error: 'svg_invalid' }; }
  const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
  const root = doc.documentElement;
  if (!root || doc.getElementsByTagName('parsererror').length
      || root.localName !== 'svg' || root.namespaceURI !== SVG_NS) {
    return { error: 'svg_invalid' };
  }
  if (doc.getElementsByTagName('*').length > maxElements) {
    return { error: 'svg_too_complex' };
  }
  // References to other documents never load in an image; Chromium would
  // draw a broken-image sign for them: they are taken out, and counted.
  let leftOut = 0;
  for (const el of [...doc.getElementsByTagName('*')]) {
    for (const attr of [...el.attributes]) {
      if (attr.localName !== 'href') continue;
      const value = attr.value.trim();
      if (value.startsWith('#') || /^data:image\\/(png|jpeg|gif|webp);/i.test(value)) continue;
      leftOut++;
      if (['image', 'use', 'feImage'].includes(el.localName)) { el.remove(); break; }
      el.removeAttributeNode(attr);
    }
  }
  const units = { '': 1, px: 1, pt: 4 / 3, pc: 16, mm: 96 / 25.4, cm: 96 / 2.54, in: 96 };
  const length = (name) => {
    const m = /^\\s*([0-9]*\\.?[0-9]+(?:e[-+]?[0-9]+)?)\\s*(px|pt|pc|mm|cm|in)?\\s*$/i
      .exec(root.getAttribute(name) || '');
    if (!m) return 0;
    const value = parseFloat(m[1]) * units[(m[2] || '').toLowerCase()];
    return isFinite(value) && value > 0 ? value : 0;
  };
  let box = null;
  const vb = (root.getAttribute('viewBox') || '').trim().split(/[\\s,]+/).map(Number);
  if (vb.length === 4 && vb.every(isFinite) && vb[2] > 0 && vb[3] > 0) box = vb;
  let w = length('width'), h = length('height');
  if (!box && w && h) {
    // Without a viewBox the drawing would not scale: its user unit is the
    // CSS pixel, so the box is its size in pixels.
    root.setAttribute('viewBox', `0 0 ${w} ${h}`);
  }
  if (box && !(w && h)) {
    if (w) h = w * box[3] / box[2];
    else if (h) w = h * box[2] / box[3];
    else { w = box[2]; h = box[3]; }
  }
  if (!(w && h)) { w = 300; h = 150; }
  const longest = Math.max(w, h);
  const target = Math.min(maxSide, Math.max(minSide, longest));
  const width = Math.max(1, Math.round(w * target / longest));
  const height = Math.max(1, Math.round(h * target / longest));
  root.setAttribute('width', String(width));
  root.setAttribute('height', String(height));
  root.setAttribute('preserveAspectRatio', root.getAttribute('preserveAspectRatio') || 'xMidYMid meet');
  const svg = new XMLSerializer().serializeToString(root);
  return { width, height, svg, leftOut };
}
"""

# Show the prepared SVG as an image and wait until it is decoded.
SHOW = """
async ([svg, width, height]) => {
  document.body.textContent = '';
  const bytes = new TextEncoder().encode(svg);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 32768) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 32768));
  }
  const img = new Image(width, height);
  img.id = 'svg';
  img.alt = '';
  img.src = 'data:image/svg+xml;base64,' + btoa(binary);
  document.body.appendChild(img);
  try { await img.decode(); } catch (e) { return false; }
  return img.naturalWidth > 0 || img.complete;
}
"""

# The share of painted pixels in a captured PNG (0 for a blank drawing).
PAINTED = """
async (png) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + png;
  await img.decode();
  const canvas = document.createElement('canvas');
  canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let painted = 0;
  for (let i = 3; i < data.length; i += 4) if (data[i] > 8) painted++;
  return painted / (data.length / 4);
}
"""


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    settings: dict[str, Any] = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    items = json.loads(sys.stdin.buffer.read() or b"{}").get("items") or []
    max_side = int(settings.get("max_side", 1024))
    min_side = int(settings.get("min_side", 256))
    max_elements = int(settings.get("max_elements", 40000))
    item_ms = int(settings.get("item_ms", 5000))
    from playwright.sync_api import Error, Route, sync_playwright

    blocked: list[str] = []
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": HOST_CSP,
        "Cache-Control": "no-store",
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--js-flags=--max-old-space-size=192",
            ],
        )
        emit({"stage": "launched"})
        page = browser.new_page(viewport={"width": 1024, "height": 1024})
        page.set_default_timeout(item_ms)

        def route(request_route: Route) -> None:
            url = request_route.request.url
            if url == HOST_URL:
                request_route.fulfill(status=200, headers=headers, body=HOST_PAGE)
                return
            if not url.startswith("data:"):
                blocked.append(url[:120])
            request_route.abort()

        page.route("**/*", route)
        page.goto(HOST_URL, wait_until="load", timeout=item_ms)
        for index, source in enumerate(items):
            try:
                prepared = page.evaluate(
                    PREPARE, [str(source), max_side, min_side, max_elements]
                )
                if prepared.get("error"):
                    emit({"item": index, "error": str(prepared["error"])})
                    continue
                width, height = int(prepared["width"]), int(prepared["height"])
                page.set_viewport_size(
                    {"width": max(width, 16), "height": max(height, 16)}
                )
                if not page.evaluate(SHOW, [prepared["svg"], width, height]):
                    emit({"item": index, "error": "svg_render_failed"})
                    continue
                png = page.locator("#svg").screenshot(
                    type="png", omit_background=True, animations="disabled"
                )
                encoded = base64.b64encode(png).decode()
                if page.evaluate(PAINTED, encoded) <= 0:
                    emit({"item": index, "error": "svg_blank"})
                    continue
                emit(
                    {
                        "item": index,
                        "png": encoded,
                        "width": width,
                        "height": height,
                        "left_out": int(prepared.get("leftOut") or 0),
                    }
                )
            except Error:
                emit({"item": index, "error": "svg_render_failed"})
        browser.close()
    emit({"done": {"blocked": blocked[:8]}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
