"""Real browser check of the public guest instance: send a prompt, wait for the answer, report whether images really render."""
import os, sys, re
from playwright.sync_api import sync_playwright

user, pw = os.environ["PROBE_USER"], os.environ["PROBE_PW"]
prompt, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900}, http_credentials={"username": user, "password": pw})
    pg = ctx.new_page()
    pg.goto("https://atlas.indidea.org/", wait_until="domcontentloaded")
    pg.wait_for_selector("#chat-input", timeout=90000)
    try:
        pg.get_by_role("button", name=re.compile("Okay", re.I)).click(timeout=4000)
    except Exception:
        pass
    pg.click("#chat-input")
    pg.keyboard.insert_text(prompt)
    pg.keyboard.press("Enter")
    pg.wait_for_selector("#response-content-container img, .markdown-prose img, .chat-assistant img", timeout=240000)
    pg.wait_for_timeout(6000)
    info = pg.evaluate("""() => [...document.querySelectorAll('img')].filter(i => /\\/images\\//.test(i.src)).map(i => ({src: i.src.replace(/[0-9a-f]{32}/, '<id>'), complete: i.complete, w: i.naturalWidth, h: i.naturalHeight}))""")
    print("images in the page:", info)
    print("'Image unavailable' shown:", pg.locator("text=Image unavailable").count())
    pg.screenshot(path=shot)
    b.close()
