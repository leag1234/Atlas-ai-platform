"""Standalone keyless Chromium check; browser binaries are development-only."""

import unittest


class TrialLocaleForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_trial_overlay_forwards_openwebui_interface_locale(self) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as browser_tool:
            browser = await browser_tool.chromium.launch(args=["--no-sandbox"])
            try:
                page = await browser.new_page(locale="de-DE")
                await page.route(
                    "**/*", lambda route: route.fulfill(body="<html lang='fr'></html>")
                )
                await page.goto("http://trial.test/")
                await page.evaluate(
                    """() => {
                      window.sent = [];
                      window.fetch = async (input, options) => {
                        window.sent.push(JSON.parse(options.body));
                        return new Response('{}');
                      };
                    }"""
                )
                await page.add_script_tag(path="services/orchestrator/chat-ui.js")
                sent = await page.evaluate(
                    """async () => {
                      const body = JSON.stringify({messages: [{role: 'user', content: 'Bonjour'}]});
                      for (const [locale, path] of [
                        ['en-US', '/api/chat/completions'],
                        ['de-DE', '/api/v1/chat/completions'],
                        ['nl-NL', '/api/chat/completions'],
                        ['ja-JP', '/api/chat/completions'],
                        ['en-US', '/api/v1/files/']
                      ]) {
                        localStorage.setItem('locale', locale);
                        await fetch(path, {method: 'POST', body});
                      }
                      localStorage.removeItem('locale');
                      await fetch('/api/chat/completions', {method: 'POST', body});
                      return window.sent;
                    }"""
                )
                self.assertEqual(
                    [body.get("ui_locale") for body in sent],
                    ["en", "de", "nl", None, None, "fr"],
                )
                for body in sent:
                    self.assertEqual(
                        body["messages"], [{"role": "user", "content": "Bonjour"}]
                    )
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
