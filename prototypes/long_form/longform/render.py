"""Mermaid validation with the real Mermaid parser, and HTML/PDF rendering."""

from __future__ import annotations

import html
import re
from pathlib import Path

from markdown_it import MarkdownIt
from playwright.sync_api import sync_playwright

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "mermaid.min.js"

CSS = """
:root { --ink:#1d2330; --muted:#5b6475; --line:#d9dee7; --accent:#2f5fb3; --bg:#ffffff; }
body { font-family: "DejaVu Sans", "Liberation Sans", Arial, sans-serif; color: var(--ink); background: var(--bg);
       max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.55; font-size: 15px; }
h1 { font-size: 1.7rem; line-height: 1.25; margin-bottom: .3rem; }
h2 { font-size: 1.25rem; margin-top: 2rem; border-bottom: 1px solid var(--line); padding-bottom: .2rem; }
h3 { font-size: 1.05rem; margin-top: 1.4rem; }
.meta { color: var(--muted); font-size: .85rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .9rem; }
th, td { border: 1px solid var(--line); padding: .35rem .5rem; text-align: left; vertical-align: top; }
th { background: #f3f5f9; }
pre.mermaid { background: none; text-align: center; margin: 1.2rem 0 .4rem; }
a { color: var(--accent); text-decoration: none; }
sup.cite { font-size: .72em; }
code { font-size: .9em; }
blockquote { border-left: 3px solid var(--line); margin-left: 0; padding-left: 1rem; color: var(--muted); }
@media print { body { margin: 0 auto; } h2 { break-after: avoid; } pre.mermaid, table { break-inside: avoid; } }
"""


def markdown_to_html(markdown: str, title: str, meta: str) -> str:
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    default_fence = md.renderer.rules.get("fence")

    def fence(tokens, idx, options, env):
        token = tokens[idx]
        if token.info.strip().lower() == "mermaid":
            return f'<pre class="mermaid">{html.escape(token.content)}</pre>\n'
        return default_fence(tokens, idx, options, env)

    md.renderer.rules["fence"] = fence
    body = md.render(markdown)
    body = re.sub(r'<a href="#((?:src|calc)-\d+)">\[([^\]<]+)\]</a>', r'<sup class="cite"><a href="#\1">[\2]</a></sup>', body)
    body = re.sub(r'<h2>([^<]*)\{#([\w-]+)\}</h2>', r'<h2 id="\2">\1</h2>', body)
    body = re.sub(r"<li>\{#(src-\d+|calc-\d+)\}\s*", r'<li id="\1">', body)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        f"<p class=\"meta\">{html.escape(meta)}</p>{body}"
        "<script src=\"MERMAID_SRC\"></script>"
        "<script>mermaid.initialize({startOnLoad:false, theme:'neutral', securityLevel:'strict', flowchart:{htmlLabels:false}});"
        "window.__done = mermaid.run({querySelector:'pre.mermaid'}).then(()=>true).catch(e=>String(e));</script>"
        "</body></html>"
    )


def validate_mermaid(sources: list[str]) -> dict[str, str]:
    """Parse each diagram with Mermaid itself. Returns {source: error} for the invalid ones."""
    if not sources:
        return {}
    errors: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(path=str(VENDOR))
        page.evaluate("mermaid.initialize({startOnLoad:false})")
        for source in sources:
            result = page.evaluate(
                "async (src) => { try { await mermaid.parse(src); return ''; } catch (e) { return String(e.message || e); } }",
                source,
            )
            if result:
                errors[source] = result
        browser.close()
    return errors


def render(markdown: str, title: str, meta: str, out_dir: Path) -> dict:
    """Write doc.html (diagrams pre-rendered as SVG, no script) and doc.pdf."""
    page_html = markdown_to_html(markdown, title, meta)
    work = out_dir / "_render.html"
    work.write_text(page_html.replace("MERMAID_SRC", VENDOR.as_uri()))
    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 1200})
        page.goto(work.as_uri())
        outcome = page.evaluate("async () => await window.__done")
        result["mermaid_render"] = outcome
        result["svg_count"] = page.evaluate("document.querySelectorAll('pre.mermaid svg').length")
        page.evaluate("document.querySelectorAll('script').forEach(s => s.remove())")
        (out_dir / "doc.html").write_text("<!doctype html>" + page.content().split("<!DOCTYPE html>", 1)[-1])
        page.pdf(path=str(out_dir / "doc.pdf"), format="A4", margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"}, print_background=True)
        browser.close()
    work.unlink()
    return result
