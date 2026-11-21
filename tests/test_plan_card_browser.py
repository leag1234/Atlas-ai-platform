"""The plan card of a long chat turn in a browser (chat-ui.js, work_plan.py).

Staging 2026-11-20: a 4-minute presentation turn showed its plan card, then the
card went away as soon as the first web search detail was written into the
message, and the plan came back only at the end as a plain Markdown list in a
different style. The cause: the card was removed once the message content was
not empty, and the search details are content (chat_stream.py, tool_details).

This test loads the real chat-ui.js and chat-ui.css in a page laid out like an
Open WebUI 0.13 message (the status line, then #response-content-container
holding .markdown-prose) and plays the Socket.IO frames the turn sends. No
container, no model. Skipped when no browser can be started (CI installs the
playwright package but no browser).
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "services/orchestrator/chat-ui.js").read_text()
STYLE = (ROOT / "services/orchestrator/chat-ui.css").read_text()
ORIGIN = "http://atlas.test"

# A socket the overlay wraps (it only listens to Socket.IO "events" frames).
FAKE_SOCKET = """
window.WebSocket = class extends EventTarget {
  constructor(url) { super(); this.url = url; window.atlasTestSocket = this; }
  send() {}
  close() {}
};
"""

PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><link rel="stylesheet" href="/chat-ui.css">
<script src="/fake-socket.js"></script><script src="/chat-ui.js"></script></head>
<body>
<div id="message-m1"><div class="chat-assistant w-full">
  <div class="status-description">Recherche web…</div>
  <div class="w-full flex flex-col relative" id="response-content-container"></div>
</div></div>
<div id="message-m2"><div class="chat-assistant w-full">
  <div class="w-full flex flex-col relative" id="response-content-container"></div>
</div></div>
</body></html>"""

STEPS = [
    "Rechercher les sources",
    "Rédiger le contenu",
    "Vérifier le contenu et la mise en page",
    "Vous livrer la présentation",
]

FINAL = (
    "<p>La présentation est prête.</p>"
    "<p><strong>Plan</strong> (terminé en 3 min 57 s)</p>"
    "<ul><li>✓ Rechercher les sources</li><li>✓ Rédiger le contenu</li>"
    "<li>⊘ Vérifier le contenu et la mise en page</li>"
    "<li>✓ Vous livrer la présentation</li></ul>"
)


def plan(version: int, statuses: list[str]) -> dict[str, Any]:
    return {
        "version": version,
        "title": "Plan (2 à 5 min)",
        "finished": False,
        "steps": [
            {"number": n, "text": text, "status": status, "state": status}
            for n, (text, status) in enumerate(zip(STEPS, statuses), 1)
        ],
    }


def status(data: dict[str, Any], message: str = "m1") -> dict[str, Any]:
    return {"message_id": message, "data": {"type": "status", "data": data}}


def activity(plan_data: dict[str, Any] | None, done: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "description": "Recherche web…",
        "done": done,
        "action": "atlas_activity",
    }
    if plan_data is not None:
        data["atlas_plan"] = plan_data
    return status(data)


class PlanCardBrowserTest(unittest.TestCase):
    playwright: Any = None
    browser: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright

            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch()
        except Exception as error:  # noqa: BLE001 - no browser on this machine
            if cls.playwright is not None:
                cls.playwright.stop()
            raise unittest.SkipTest(f"no browser: {type(error).__name__}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        # A French browser: the interface (and the state words) are French.
        self.page = self.browser.new_page(locale="fr-FR")
        self.errors: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        files = {
            "/": ("text/html", PAGE),
            "/chat-ui.css": ("text/css", STYLE),
            "/chat-ui.js": ("text/javascript", SCRIPT),
            "/fake-socket.js": ("text/javascript", FAKE_SOCKET),
        }

        def serve(route: Any) -> None:
            path = route.request.url.removeprefix(ORIGIN).split("?")[0]
            kind, body = files.get(path, ("text/plain", ""))
            route.fulfill(
                status=200 if path in files else 404,
                content_type=kind + "; charset=utf-8",
                body=body,
            )

        self.page.route("**/*", serve)
        self.page.goto(ORIGIN + "/")
        self.page.evaluate(
            "() => { new WebSocket('ws://atlas.test/ws/socket.io/?EIO=4&transport=websocket'); }"
        )

    def tearDown(self) -> None:
        self.page.close()

    def send(self, payload: dict[str, Any]) -> None:
        frame = "42" + json.dumps(["events", payload], ensure_ascii=False)
        self.page.evaluate(
            "frame => window.atlasTestSocket.dispatchEvent(new MessageEvent('message', { data: frame }))",
            frame,
        )
        self.settle()

    def settle(self) -> None:
        self.page.evaluate(
            "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
        )

    def content(self, html: str, message: str = "m1") -> None:
        """What Open WebUI draws for the message content: .markdown-prose."""
        self.page.evaluate(
            """([id, html]) => {
                const host = document.querySelector('#message-' + id + ' [id="response-content-container"]');
                let prose = host.querySelector('.markdown-prose');
                if (!prose) { prose = document.createElement('div'); prose.className = 'markdown-prose'; host.append(prose); }
                prose.insertAdjacentHTML('beforeend', html);
            }""",
            [message, html],
        )
        self.settle()

    def card(self, message: str = "m1") -> dict[str, Any] | None:
        found: dict[str, Any] | None = self.page.evaluate(
            """id => {
                const host = document.querySelector('#message-' + id + ' [id="response-content-container"]');
                const cards = host.querySelectorAll('.atlas-plan');
                if (!cards.length) return null;
                const card = cards[0], style = getComputedStyle(card);
                return {
                    count: cards.length,
                    first: host.firstElementChild === card,
                    final: card.dataset.final === 'true',
                    title: card.querySelector('.atlas-plan-title').textContent,
                    statuses: [...card.querySelectorAll('li')].map(li => li.dataset.status),
                    texts: [...card.querySelectorAll('.atlas-plan-text')].map(t => t.lastChild.textContent),
                    states: [...card.querySelectorAll('.atlas-plan-state')].map(t => t.textContent.trim()),
                    style: [style.borderTopWidth, style.borderRadius, style.paddingLeft, style.fontSize].join(' '),
                };
            }""",
            message,
        )
        return found

    def test_the_card_stays_through_searches_and_ends_checked_in_place(self) -> None:
        self.send(activity(plan(1, ["in_progress", "pending", "pending", "pending"])))
        live = self.card()
        assert live is not None
        self.assertTrue(live["first"])
        self.assertEqual(live["statuses"][0], "in_progress")
        # A web search detail is message content: the card stays above it.
        self.content(
            "<details><summary>Recherche web</summary>https://example.org</details>"
        )
        # A status without a plan (the search line) leaves the card as it is.
        self.send(activity(None))
        after = self.card()
        assert after is not None
        self.assertTrue(after["first"])
        self.assertEqual(after["statuses"], live["statuses"])
        # The next state is drawn in the same card.
        self.send(activity(plan(2, ["completed", "in_progress", "pending", "pending"])))
        updated = self.card()
        assert updated is not None
        self.assertEqual(updated["count"], 1)
        self.assertEqual(updated["statuses"][:2], ["completed", "in_progress"])
        # An older plan sent late never brings back an older state.
        self.send(activity(plan(1, ["in_progress", "pending", "pending", "pending"])))
        self.assertEqual(self.card()["statuses"][:2], ["completed", "in_progress"])  # type: ignore[index]
        # The answer starts: the status is done, the card stays.
        self.send(activity(None, done=True))
        self.assertIsNotNone(self.card())
        # The answer ends with the checked plan: drawn by the same card, at the top.
        self.content(FINAL)
        self.send(
            {
                "message_id": "m1",
                "data": {"type": "chat:completion", "data": {"done": True}},
            }
        )
        final = self.card()
        assert final is not None
        self.assertEqual(final["count"], 1)
        self.assertTrue(final["first"] and final["final"])
        self.assertEqual(final["title"], "Plan (terminé en 3 min 57 s)")
        self.assertEqual(
            final["statuses"], ["completed", "completed", "skipped", "completed"]
        )
        self.assertEqual(final["texts"], STEPS)
        self.assertEqual(final["states"][2], "non fait")
        self.assertEqual(final["style"], live["style"])
        # The Markdown copy is hidden; the answer itself is not.
        hidden = self.page.evaluate(
            """() => [...document.querySelectorAll('#message-m1 .markdown-prose > *')]
                .map(node => [node.tagName, node.hidden])"""
        )
        self.assertEqual(
            hidden,
            [["DETAILS", False], ["P", False], ["P", True], ["UL", True]],
        )
        self.page.wait_for_timeout(1800)
        self.settle()
        self.assertIsNotNone(self.card())
        self.assertEqual(self.errors, [])

    def test_a_conversation_opened_again_shows_the_same_card(self) -> None:
        # No socket frame: the stored answer alone.
        self.content(FINAL, "m2")
        final = self.card("m2")
        assert final is not None
        self.assertTrue(final["first"] and final["final"])
        self.assertEqual(
            final["statuses"], ["completed", "completed", "skipped", "completed"]
        )
        self.assertEqual(self.errors, [])

    def test_a_turn_ending_without_its_plan_loses_the_card(self) -> None:
        self.send(activity(plan(1, ["in_progress", "pending", "pending", "pending"])))
        self.assertIsNotNone(self.card())
        self.content("<p>L’application n’a pas pu être créée.</p>")
        self.send(
            {
                "message_id": "m1",
                "data": {"type": "chat:completion", "data": {"done": True}},
            }
        )
        self.page.wait_for_timeout(1800)
        self.settle()
        self.assertIsNone(self.card())

    def test_an_ordinary_list_is_not_a_plan(self) -> None:
        self.content(
            "<p><strong>Points</strong> à retenir</p>"
            "<ul><li>✓ le train part à 8 h</li><li>✓ le retour à 18 h</li></ul>",
            "m2",
        )
        self.assertIsNone(self.card("m2"))
        shown = self.page.evaluate(
            "() => [...document.querySelectorAll('#message-m2 .markdown-prose > *')].map(n => n.hidden)"
        )
        self.assertEqual(shown, [False, False])


if __name__ == "__main__":
    unittest.main()
