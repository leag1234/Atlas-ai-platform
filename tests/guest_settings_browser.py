"""Second-pass owner UI regression contracts (optional Playwright runner)."""

from pathlib import Path
import json
import unittest

from playwright.sync_api import Route, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "flex items-center gap-1.5 h-7 px-2 md:w-full shrink-0 rounded-lg text-xs text-left transition-colors duration-75 "
IDLE = BASE + "text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
ACTIVE = (
    BASE + "font-medium text-gray-900 dark:text-white bg-gray-50 dark:bg-white/[0.04]"
)


class GuestNavigationTests(unittest.TestCase):
    def test_native_navigation_group_icons_states_and_unique_theme_control(
        self,
    ) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(locale="fr-FR")
                html = f"""<html><head><meta charset="utf-8"></head><body><nav id="settings-tabs-container">
                <input id="search-input-settings-modal"><div class="tabs">
                <button role="tab" aria-controls="tab-general" aria-selected="true" class="{ACTIVE}">General</button>
                <span>Préférences</span>
                <button role="tab" aria-controls="tab-audio" aria-selected="false" class="{IDLE}">Audio</button>
                </div></nav><select aria-label="Thème"><option>Clair</option></select></body></html>"""

                def respond(route: Route) -> None:
                    if "/api/atlas/user-memory" in route.request.url:
                        route.fulfill(
                            json={
                                "policy_enabled": True,
                                "enabled": True,
                                "effective_enabled": True,
                                "entries": [],
                            }
                        )
                    else:
                        route.fulfill(body=html, content_type="text/html")

                page.route("**/*", respond)
                page.goto("http://guest.test/")
                page.add_style_tag(path=str(ROOT / "services/orchestrator/chat-ui.css"))
                page.set_viewport_size({"width": 390, "height": 844})
                for name in ("chat-ui.js", "user_memory.js"):
                    page.add_script_tag(path=str(ROOT / "services/orchestrator" / name))
                appearance = page.locator('[data-atlas-settings="appearance"]')
                memory = page.locator('[data-atlas-settings="memory"]')
                expect(appearance).to_have_class(IDLE)
                expect(memory).to_have_class(IDLE)
                expect(appearance.locator('svg[aria-hidden="true"]')).to_have_count(1)
                expect(memory.locator('svg[aria-hidden="true"]')).to_have_count(1)
                self.assertEqual(
                    page.locator(".tabs").evaluate(
                        "e=>[...e.children].map(n=>n.textContent)"
                    ),
                    ["General", "Préférences", "Apparence", "Mémoire", "Audio"],
                )
                appearance.click()
                expect(appearance).to_have_class(ACTIVE)
                expect(appearance).to_have_attribute("aria-expanded", "true")
                expect(page.locator('[data-atlas-option="mode"]')).to_have_count(0)
                for group in ("style", "accent", "size"):
                    self.assertGreater(
                        page.locator(f'[data-atlas-option="{group}"]').count(), 0
                    )
                self.assertTrue(
                    page.locator("#atlas-appearance-dialog").evaluate(
                        """e=>[...e.querySelectorAll('.atlas-options')].every(row=>
                        [...row.children].every(button=>button.getBoundingClientRect().right <= row.getBoundingClientRect().right + 1))"""
                    )
                )
                page.keyboard.press("Escape")
                expect(appearance).to_be_focused()
                expect(appearance).to_have_class(IDLE)
                memory.click()
                expect(memory).to_have_class(ACTIVE)
                expect(page.get_by_text("Aucun souvenir enregistré.")).to_be_visible()
                page.keyboard.press("Escape")
                expect(memory).to_be_focused()
                expect(memory).to_have_class(IDLE)
                page.evaluate("localStorage.locale='de-DE'")
                page.locator('[aria-controls="tab-audio"]').evaluate(
                    "e=>e.textContent='Audio ändern'"
                )
                expect(memory).to_have_text("Erinnerungen")
                expect(appearance).to_have_text("Darstellung")
            finally:
                browser.close()

    def test_stable_filter_preserves_independent_api_key_entry_and_notes(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                source = (ROOT / "services/orchestrator/settings-ui.js").read_text()
                # Use the real native-store interface with a test-only store.
                native = """const instance={isInitialized:true,on(){},hasResourceBundle(){return true;},addResourceBundle(){}};
                export const i={subscribe(fn){fn(instance);},set(){}};"""

                def respond(route: Route) -> None:
                    url = route.request.url
                    if url.endswith("settings-ui.js"):
                        route.fulfill(body=source, content_type="text/javascript")
                    elif url.endswith("DKxluP3e.js"):
                        route.fulfill(body=native, content_type="text/javascript")
                    elif url.endswith("settings-locales.json"):
                        route.fulfill(
                            body=json.dumps({}), content_type="application/json"
                        )
                    elif url.endswith("stable-auth.js"):
                        route.fulfill(body="", content_type="text/javascript")
                    else:
                        route.fulfill(
                            content_type="text/html",
                            body="""<html><head><meta charset="utf-8"><script src="/static/atlas/stable-auth.js"></script>
                            <script type="module" src="/static/atlas/settings-ui.js"></script></head><body>
                            <div class="app-dropdown-menu"><div class="user-menu-status"><button>Status</button></div>
                            <div><a href="/calendar">Calendar</a></div><div><a href="/automations">Automations</a></div>
                            <div><a href="/notes">Notes</a></div><button>Réglages</button><button>Clés API</button><button>Déconnexion</button></div>
                            </body></html>""",
                        )

                page.route("**/*", respond)
                page.goto("http://guest.test/")
                expect(page.locator("#atlas-stable-menu-style")).to_have_count(1)
                for name in ("Status", "Calendar", "Automations"):
                    expect(page.get_by_text(name, exact=True)).to_be_hidden()
                for name in ("Notes", "Réglages", "Clés API", "Déconnexion"):
                    expect(page.get_by_text(name, exact=True)).to_be_visible()
            finally:
                browser.close()
