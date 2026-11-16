"""Deterministic network and native-DOM contracts for the guest enhancement.

A closed socket or a silent stream only makes a turn suspect: the script asks the
server first, waits while the task runs, keeps an answer saved complete, and shows
the interruption (and stops) only when the server no longer runs or holds the turn.
"""

import unittest

from playwright.sync_api import sync_playwright, expect


SHELL = """<html><head><script src="/static/atlas/stable-auth.js"></script></head><body>
<div id="message-a"><div class="chat-assistant"><div id="response-content-container"><button aria-expanded="false"><span class="status-description">Thinking</span></button><p id="partial-answer">Existing answer text.</p><button id="partial-details" aria-expanded="false">Sources</button></div></div><div class="buttons"><button class="regenerate-response-button" onclick="window.retried=true">Regenerate</button></div></div>
<button id="stop-response-button" onclick="this.remove()">Stop</button>
<div id="message-input-container"><div contenteditable id="chat-input"><p class="is-empty" data-placeholder="How can I help you today?"></p></div></div>
</body></html>"""


class RecoveryTests(unittest.TestCase):
    def test_silence_disconnect_http_failure_completion_and_retry(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for failure in (
                "silence",
                "silence_running",
                "socket",
                "socket_running",
                "socket_saved",
                "fresh_socket",
                "fresh_done",
                "late_completion",
                "http",
                "terminal_error",
                "cancel",
                "done",
                "heartbeat",
            ):
                with self.subTest(failure=failure):
                    page = browser.new_page(locale="fr-FR")
                    page.route(
                        "**/*",
                        lambda route: route.fulfill(
                            content_type="text/html", body=SHELL
                        ),
                    )
                    page.route(
                        "**/stable-auth.js", lambda route: route.fulfill(body="")
                    )
                    page.route(
                        "**/atlas-version.json",
                        lambda route: route.fulfill(
                            content_type="application/json", body='{"version":"0.6.0"}'
                        ),
                    )
                    page.route(
                        "**/api/chat/completions",
                        lambda route: route.fulfill(
                            status=503 if failure == "http" else 200,
                            content_type="application/json",
                            body="{}",
                        ),
                    )
                    running = failure.endswith("_running")
                    requests: list[str] = []
                    page.on("request", lambda request: requests.append(request.url))
                    tasks = '{"task_ids":["t1"]}' if running else '{"task_ids":[]}'
                    stored = '{"chat":{"history":{"messages":{"a":{"done":%s}}}}}' % (
                        "true" if failure == "socket_saved" else "false"
                    )
                    page.route(
                        "**/api/tasks/chat/**",
                        lambda route: route.fulfill(
                            content_type="application/json", body=tasks
                        ),
                    )
                    page.route(
                        "**/api/v1/chats/**",
                        lambda route: route.fulfill(
                            content_type="application/json", body=stored
                        ),
                    )
                    page.goto("http://guest.test/")
                    page.clock.install()
                    page.evaluate("""() => {
                      window.WebSocket = class extends EventTarget { constructor() {super(); window.testSocket=this;} };
                      localStorage.setItem('locale','fr-FR');
                    }""")
                    page.add_style_tag(path="services/orchestrator/chat-ui.css")
                    page.add_script_tag(path="services/orchestrator/guest-chat.js")
                    page.evaluate(
                        """async fresh => {
                      new WebSocket('ws://guest.test/ws/socket.io');
                      await fetch('/api/chat/completions',{method:'POST',body:JSON.stringify({id:'a',chat_id:fresh?undefined:'c'})});
                    }""",
                        failure.startswith("fresh_"),
                    )
                    if failure in ("socket_running", "silence_running", "socket_saved"):
                        if failure != "silence_running":
                            page.evaluate(
                                "testSocket.dispatchEvent(new Event('close'))"
                            )
                        for _ in range(4):
                            page.clock.fast_forward(46000)
                            expect(page.locator(".atlas-interruption")).to_have_count(0)
                        # The answer keeps running (or was saved): nothing stopped it.
                        expect(page.locator("#stop-response-button")).to_have_count(1)
                        self.assertFalse([u for u in requests if u.endswith("/stop")])
                        self.assertTrue(
                            [u for u in requests if "/api/tasks/chat/c" in u]
                        )
                        page.close()
                        continue
                    if failure in ("socket", "fresh_socket", "late_completion"):
                        page.evaluate("testSocket.dispatchEvent(new Event('close'))")
                        # Socket.IO reconnects within seconds: no notice before the grace period.
                        page.clock.fast_forward(10000)
                        expect(page.locator(".atlas-interruption")).to_have_count(0)
                        expect(page.locator("#stop-response-button")).to_have_count(1)
                        page.clock.fast_forward(6000)
                    elif failure == "terminal_error":
                        page.evaluate(
                            """() => testSocket.dispatchEvent(new MessageEvent('message', {data:'42'+JSON.stringify(['events',{chat_id:'c', message_id:'a', data:{type:'chat:completion', data:{done:true,error:'connection lost'}}}])}))"""
                        )
                    elif failure == "cancel":
                        page.locator("#stop-response-button").click()
                        page.clock.fast_forward(60000)
                        expect(page.locator(".atlas-interruption")).to_have_count(0)
                        page.close()
                        continue
                    elif failure in ("done", "fresh_done", "heartbeat"):
                        page.clock.fast_forward(44000)
                        page.evaluate(
                            """done => testSocket.dispatchEvent(new MessageEvent('message', {data:'42'+JSON.stringify(['events',{chat_id:'c', message_id:'a', data:{type:done?'chat:completion':'status', data:{done}}}])}))""",
                            failure in ("done", "fresh_done"),
                        )
                        page.clock.fast_forward(44000)
                        expect(page.locator(".atlas-interruption")).to_have_count(0)
                        if failure in ("done", "fresh_done"):
                            page.clock.fast_forward(60000)
                            expect(page.locator(".atlas-interruption")).to_have_count(0)
                            page.close()
                            continue
                        page.clock.fast_forward(2000)
                    elif failure == "silence":
                        page.clock.fast_forward(44000)
                        expect(page.locator(".atlas-interruption")).to_have_count(0)
                        page.clock.fast_forward(2000)
                    notice = page.locator(".atlas-interruption")
                    expect(notice).to_contain_text(
                        "La r\u00e9ponse a \u00e9t\u00e9 interrompue."
                    )
                    if failure == "late_completion":
                        page.evaluate(
                            """() => testSocket.dispatchEvent(new MessageEvent('message', {data:'42'+JSON.stringify(['events',{chat_id:'c', message_id:'a', data:{type:'chat:completion', data:{done:true}}}])}))"""
                        )
                        expect(notice).to_be_visible()
                    expect(page.locator(".status-description")).not_to_be_visible()
                    expect(page.locator("#partial-answer")).to_be_visible()
                    expect(page.locator("#partial-details")).to_be_visible()
                    retry = notice.get_by_role("button", name="R\u00e9essayer")
                    focus_styles = []
                    for control in (retry, page.locator("#chat-input")):
                        control.focus()
                        focus_styles.append(
                            control.evaluate(
                                "e=>{const s=getComputedStyle(e);return [s.outlineColor,s.outlineWidth,s.outlineStyle]}"
                            )
                        )
                    self.assertEqual(
                        focus_styles, [["rgb(194, 65, 12)", "2px", "solid"]] * 2
                    )
                    retry.click()
                    self.assertTrue(page.evaluate("window.retried"))
                    expect(notice).to_have_count(0)
                    page.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
