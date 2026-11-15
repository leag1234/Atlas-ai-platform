"""A failed code download while the chat starts is recovered by one reload (chat-ui.js).

Defect of 2026-11-14: when one of Open WebUI's code modules fails to download while the page starts
(measured: net::ERR_NETWORK_CHANGED on the build VM; for users, a phone switching networks), the app
stops on an empty page or on "500: Internal Error", without a composer. The shell below follows the
two structures both Open WebUI releases use: the entry modules imported by an inline script, and the
route modules imported through Vite's preload helper, which dispatches vite:preloadError.
Standalone keyless Chromium check; browser binaries are development-only.
"""

from pathlib import Path
import unittest

from playwright.sync_api import Route, sync_playwright

CHAT_UI = Path("services/orchestrator/chat-ui.js").read_text()

SHELL = """<!doctype html><html><head><script src="/static/atlas/chat-ui.js"></script></head><body>
<div id="app"></div>
<script>
  // Vite's preload helper, as shipped by Open WebUI 0.11.3 and 0.13.3 (dependencies left out).
  const preload = importer => importer().catch(error => {
    const event = new Event("vite:preloadError", {cancelable: true});
    event.payload = error;
    window.dispatchEvent(event);
    if (!event.defaultPrevented) throw error;
  });
  window.startRoute = () => preload(() => import("/_app/nodes/2.js"))
    .then(module => module.mount())
    .catch(() => { document.getElementById("app").textContent = "500: Internal Error"; });
  // SvelteKit's start: the entry modules, without a catch (an unhandled rejection when they fail).
  Promise.all([import("/_app/entry/app.js")]).then(([app]) => app.start());
</script></body></html>"""

ENTRY = "export const start = () => window.startRoute();"
NODE = """export const mount = () => {
  const input = document.createElement("div");
  input.id = "chat-input";
  input.contentEditable = "true";
  input.textContent = " ";
  document.getElementById("app").append(input);
};"""


class CodeDownloadRecoveryTests(unittest.TestCase):
    def run_shell(
        self, failures: dict[str, int], wait_ms: int = 2500
    ) -> tuple[int, str]:
        """Serve the shell; each module path fails as many times as asked; return (loads, page text)."""
        loads = {"shell": 0}
        remaining = dict(failures)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()

                def serve(route: Route) -> None:
                    path = route.request.url.split("chat.test", 1)[1].split("?", 1)[0]
                    if remaining.get(path, 0) > 0:
                        remaining[path] -= 1
                        route.abort("internetdisconnected")
                    elif path == "/static/atlas/chat-ui.js":
                        route.fulfill(content_type="text/javascript", body=CHAT_UI)
                    elif path == "/_app/entry/app.js":
                        route.fulfill(content_type="text/javascript", body=ENTRY)
                    elif path == "/_app/nodes/2.js":
                        route.fulfill(content_type="text/javascript", body=NODE)
                    elif path == "/":
                        loads["shell"] += 1
                        route.fulfill(content_type="text/html", body=SHELL)
                    else:
                        route.fulfill(status=404, body="")

                page.route("**/*", serve)
                page.goto("http://chat.test/")
                page.wait_for_timeout(wait_ms)
                return loads["shell"], page.evaluate(
                    "() => document.body.innerText.trim()"
                ) + (" [composer]" if page.query_selector("#chat-input") else "")
            finally:
                browser.close()

    def test_route_module_failure_reloads_once_and_shows_the_composer(self) -> None:
        loads, text = self.run_shell({"/_app/nodes/2.js": 1})
        self.assertEqual(loads, 2)
        self.assertIn("[composer]", text)

    def test_entry_module_failure_reloads_once_and_shows_the_composer(self) -> None:
        loads, text = self.run_shell({"/_app/entry/app.js": 1})
        self.assertEqual(loads, 2)
        self.assertIn("[composer]", text)

    def test_a_lasting_failure_reloads_only_once(self) -> None:
        loads, text = self.run_shell({"/_app/nodes/2.js": 99}, wait_ms=4000)
        self.assertEqual(loads, 2)
        self.assertEqual(text, "500: Internal Error")

    def test_no_reload_once_the_composer_is_shown(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()
                loads = []
                page.on("load", lambda _page: loads.append(1))
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body=(
                            "<html><head><script>" + CHAT_UI + "</script></head>"
                            "<body><div id='chat-input' contenteditable='true'>draft</div></body></html>"
                        ),
                    ),
                )
                page.goto("http://chat.test/")
                page.evaluate(
                    "() => window.dispatchEvent(new Event('vite:preloadError', {cancelable: true}))"
                )
                page.wait_for_timeout(1000)
                self.assertEqual(len(loads), 1)
                self.assertEqual(page.inner_text("#chat-input"), "draft")
            finally:
                browser.close()

    def test_no_reload_after_the_start_window(self) -> None:
        # A lazy module failing later in the session (no composer on that view) keeps the page as it is.
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.clock.install()
                loads = []
                page.on("load", lambda _page: loads.append(1))
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body=(
                            "<html><head><script>"
                            + CHAT_UI
                            + "</script></head><body>settings</body></html>"
                        ),
                    ),
                )
                page.goto("http://chat.test/admin/settings")
                page.clock.run_for(25000)
                page.evaluate(
                    "() => window.dispatchEvent(new Event('vite:preloadError', {cancelable: true}))"
                )
                page.wait_for_timeout(1000)
                self.assertEqual(len(loads), 1)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
