"""Offline Playwright check of the device-first location flow on a phone viewport.

Owner decision 2026-11-15: when Atlas needs the person's position, the device is
asked before the person. The server's reply carries LOCATION_REQUEST; the real
chat-ui.js overlay then uses the stored "always" permission or the consent
dialog, and sends the same turn again through Open WebUI's regenerate control
with the position attached. This page reproduces only the Open WebUI parts the
overlay relies on (message ids, .chat-assistant, the hidden
.regenerate-response-button, the background-task /api/chat/completions call);
the replies are the server's own texts (location_route.py). Real Chromium
geolocation grant and denial are used; the "no geolocation" browser is the only
simulated one.

    PYTHONPATH=. python tests/location_consent_browser.py
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, Route, expect, sync_playwright

from services.orchestrator.location import Location
from services.orchestrator.location_route import device_request, no_position_reply

# A local origin: a secure context, as the real https instance (geolocation and
# crypto.randomUUID need one). Nothing listens there: every request is routed.
BASE = "http://localhost:18997"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "BRAIN" / "location-consent-browser"
CASTLE = "Le château de Valombre est ouvert cet aprèm ? et combien de temps pour y aller depuis chez moi"
ANSWER = "Réponse avec la position : Rue des Tilleuls, Caen."

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="/static/atlas/chat-ui.js"></script>
<style>body{margin:0;font:16px sans-serif} #messages{padding:16px} .hidden{display:none}</style>
</head><body><div id="messages"></div>
<form id="composer"><textarea id="chat-input"></textarea><button>Envoyer</button></form>
<script>
  // The parts of Open WebUI the overlay relies on: message ids, .chat-assistant,
  // the hidden regenerate control, and a background-task chat request.
  const history = [];
  const render = text => text.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, (all, label, href) => `<a href="${href}" target="_blank" rel="nofollow">${label}</a>`).split("\\n\\n").map(p => `<p>${p}</p>`).join("");
  async function answer(index) {
    const response = await fetch("/api/chat/completions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: "atlas", messages: history.slice(0, index + 1) }) });
    const { content } = await response.json();
    document.querySelectorAll('[data-for="' + index + '"]').forEach(node => node.remove());
    const id = "message-" + crypto.randomUUID();
    const node = document.createElement("div"); node.id = id; node.dataset.for = String(index);
    node.innerHTML = `<div class="chat-assistant w-full"><div class="markdown-prose">${render(content)}</div></div><button type="button" class="hidden regenerate-response-button"></button>`;
    node.querySelector(".regenerate-response-button").addEventListener("click", () => answer(index));
    document.getElementById("messages").append(node);
  }
  window.sendMessage = text => { history.push({ role: "user", content: text }); const node = document.createElement("div"); node.className = "chat-user"; node.textContent = text; document.getElementById("messages").append(node); return answer(history.length - 1); };
  window.showOldReply = content => { history.push({ role: "user", content: "old" }); const node = document.createElement("div"); node.id = "message-old"; node.innerHTML = `<div class="chat-assistant"><div class="markdown-prose">${render(content)}</div></div><button type="button" class="hidden regenerate-response-button"></button>`; node.querySelector("button").addEventListener("click", () => answer(history.length - 1)); document.getElementById("messages").append(node); };
  document.getElementById("composer").addEventListener("submit", event => { event.preventDefault(); const input = document.getElementById("chat-input"); window.sendMessage(input.value); input.value = ""; });
</script></body></html>"""


class Server:
    """The Atlas side: location tickets and the chat reply for each request."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self.chats: list[dict[str, Any]] = []

    def reply(self, body: dict[str, Any]) -> str:
        ticket = self.tickets.get(str(body.get("location_ref")))
        if ticket is None:
            return device_request("fr")
        location = Location.model_validate(ticket)
        if location.position is not None:
            return ANSWER
        if location.place:
            return f"Réponse depuis {location.place}."
        reason = location.failure or (
            "refused" if location.consent == "refused" else "denied"
        )
        return no_position_reply(reason, "fr")

    def handle(self, route: Route) -> None:
        request = route.request
        path = request.url.removeprefix(BASE).split("?")[0]
        if path == "/":
            route.fulfill(body=PAGE, content_type="text/html")
        elif path == "/static/atlas/chat-ui.js":
            route.fulfill(
                path=str(ROOT / "services/orchestrator/chat-ui.js"),
                content_type="text/javascript",
            )
        elif path == "/static/atlas/nearby.css":
            route.fulfill(
                path=str(ROOT / "services/orchestrator/static/nearby.css"),
                content_type="text/css",
            )
        elif path == "/api/v1/auths/":
            route.fulfill(json={"id": "user-fictional-1"})
        elif path == "/api/atlas/nearby-location" and request.method == "POST":
            body = dict(request.post_data_json or {})
            Location.model_validate(body)
            token = str(uuid.uuid4())
            self.tickets[token] = body
            route.fulfill(json={"location_ref": token})
        elif path == "/api/chat/completions":
            body = dict(request.post_data_json or {})
            self.chats.append(body)
            route.fulfill(json={"content": self.reply(body)})
        else:
            route.fulfill(status=404, json={})


def open_page(context: BrowserContext, server: Server, *, always: bool = False) -> Page:
    context.route(re.compile(r"^" + re.escape(BASE) + "/"), server.handle)
    script = 'localStorage.setItem("locale", "fr-FR");'
    if always:
        script += 'localStorage.setItem("atlas-location-permission:user-fictional-1", "always");'
    context.add_init_script(script)
    page = context.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("typeof window.atlasLocation === 'object'")
    return page


def last_answer(page: Page) -> str:
    return page.locator(".chat-assistant").last.inner_text()


def located(server: Server) -> dict[str, Any]:
    """The location attached to the second request: the same turn sent again."""
    assert len(server.chats) == 2, server.chats
    first, second = server.chats
    assert "location_ref" not in first
    assert first["messages"] == second["messages"]
    return server.tickets[second["location_ref"]]


def no_horizontal_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    phone: dict[str, Any] = dict(
        viewport={"width": 390, "height": 844},
        locale="fr-FR",
        is_mobile=True,
        has_touch=True,
    )
    receipt: dict[str, Any] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])

        # 1. Granted: the question is asked, the device is asked, the answer replaces the ask.
        server = Server()
        context = browser.new_context(
            **phone,
            geolocation={"latitude": 49.1829, "longitude": -0.3707},
            permissions=["geolocation"],
        )
        page = open_page(context, server)
        page.locator("#chat-input").fill(CASTLE)
        page.get_by_role("button", name="Envoyer").click()
        modal = page.locator("#atlas-location-consent")
        expect(modal).to_be_visible(timeout=10000)
        no_horizontal_scroll(page)
        page.screenshot(path=str(OUTPUT / "granted-consent.png"))
        modal.get_by_role("button", name="Cette fois seulement", exact=True).click()
        expect(page.locator(".chat-assistant")).to_have_count(1)
        expect(page.locator(".chat-assistant").last).to_contain_text(ANSWER)
        ticket = located(server)
        assert ticket["consent"] == "once" and ticket["position"]["latitude"] == 49.183
        assert page.locator('a[href$="#atlas-location-request"]').count() == 0
        no_horizontal_scroll(page)
        page.screenshot(path=str(OUTPUT / "granted-answer.png"))
        receipt["granted"] = True

        # 2. A device request ("demande ma position au tel") follows the same flow.
        server.chats.clear()
        page.evaluate("text => window.sendMessage(text)", "demande ma position au tel")
        expect(modal).to_be_visible(timeout=10000)
        modal.get_by_role("button", name="Toujours partager", exact=True).click()
        expect(page.locator(".chat-assistant").last).to_contain_text(ANSWER)
        assert located(server)["consent"] == "always"
        receipt["device_request"] = True
        context.close()

        # 3. Stored "always": no dialog, the position is obtained silently.
        server = Server()
        context = browser.new_context(
            **phone,
            geolocation={"latitude": 49.1829, "longitude": -0.3707},
            permissions=["geolocation"],
        )
        page = open_page(context, server, always=True)
        page.evaluate(
            "text => window.sendMessage(text)", "how long to get ther from home"
        )
        expect(page.locator(".chat-assistant").last).to_contain_text(ANSWER)
        assert page.locator("#atlas-location-consent").count() == 0
        assert located(server)["consent"] == "always"
        receipt["always_silent"] = True

        # 4. An old reply with the request (after a reload) never opens the dialog by
        # itself; its button still works.
        server.chats.clear()
        page.reload()
        page.wait_for_function("typeof window.atlasLocation === 'object'")
        page.evaluate("text => window.showOldReply(text)", device_request("fr"))
        page.wait_for_timeout(1500)
        assert (
            server.chats == [] and page.locator("#atlas-location-consent").count() == 0
        )
        button = page.locator("a.atlas-location-request")
        expect(button).to_be_visible()
        box = button.bounding_box()
        assert box is not None and box["height"] >= 44
        button.click()
        expect(page.locator(".chat-assistant").last).to_contain_text(ANSWER)
        receipt["no_auto_after_reload"] = True
        context.close()

        # 5. Denied by the browser: the reason is shown and sent, the server names it,
        # and the flow does not loop.
        server = Server()
        context = browser.new_context(**phone)
        page = open_page(context, server)
        page.evaluate("text => window.sendMessage(text)", CASTLE)
        modal = page.locator("#atlas-location-consent")
        expect(modal).to_be_visible(timeout=10000)
        modal.get_by_role("button", name="Cette fois seulement", exact=True).click()
        expect(modal.get_by_role("textbox")).to_be_visible()
        expect(modal.locator("p")).to_contain_text("refuse l’accès à la position")
        page.screenshot(path=str(OUTPUT / "denied.png"))
        modal.get_by_role("button", name="Annuler", exact=True).click()
        expect(page.locator(".chat-assistant").last).to_contain_text(
            "Votre navigateur refuse l’accès à la position"
        )
        ticket = located(server)
        assert ticket == {
            "consent": "denied",
            "failure": "permission",
            "timezone": ticket["timezone"],
        }
        page.wait_for_timeout(1500)
        assert len(server.chats) == 2
        receipt["denied"] = True

        # 6. Refused in the dialog: named as a refusal.
        server.chats.clear()
        page.evaluate(
            "text => window.sendMessage(text)", "itinéraire pour aller au stade"
        )
        expect(modal).to_be_visible(timeout=10000)
        modal.get_by_role("button", name="Refuser", exact=True).click()
        modal.get_by_role("button", name="Annuler", exact=True).click()
        expect(page.locator(".chat-assistant").last).to_contain_text(
            "Vous n’avez pas partagé votre position."
        )
        assert located(server)["consent"] == "refused"
        receipt["refused"] = True
        context.close()

        # 7. A browser without geolocation: the place field opens directly, with the reason.
        server = Server()
        context = browser.new_context(**phone)
        context.add_init_script(
            "Object.defineProperty(Navigator.prototype, 'geolocation', { get: () => undefined });"
        )
        page = open_page(context, server)
        page.evaluate("text => window.sendMessage(text)", "will it rain here tonite")
        modal = page.locator("#atlas-location-consent")
        expect(modal.get_by_role("textbox")).to_be_visible(timeout=10000)
        expect(modal.locator("p")).to_contain_text("ne fournit pas la position")
        modal.get_by_role("textbox").fill("gare de Caen")
        modal.get_by_role("button", name="Utiliser ce lieu", exact=True).click()
        expect(page.locator(".chat-assistant").last).to_contain_text(
            "Réponse depuis gare de Caen."
        )
        ticket = located(server)
        assert ticket["failure"] == "unsupported" and ticket["place"] == "gare de Caen"
        receipt["unsupported_typed_place"] = True
        context.close()
        browser.close()
    (OUTPUT / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print("Location consent browser checks complete:", ", ".join(sorted(receipt)))


if __name__ == "__main__":
    main()
