"""REQ-PROJ-08: browser contract fixtures; live acceptance is a separate runner."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from typing import Any

from playwright.sync_api import Route, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORG = "10000000-0000-4000-8000-000000000001"
PROJECT = "20000000-0000-4000-8000-000000000001"
CONVERSATION = "30000000-0000-4000-8000-000000000001"
SESSION = "40000000-0000-4000-8000-000000000001"
VERSION = "50000000-0000-4000-8000-000000000001"
BASE = f"/api/atlas/orgs/{ORG}/projects/{PROJECT}"


class ProjectComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(args=["--no-sandbox"])
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.calls: list[tuple[str, str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.respond)
        self.context.add_init_script(
            "localStorage.setItem('locale','en');"
            "window.atlasSideView={close(){},open(value){window.preview=value;}};"
        )

    def tearDown(self) -> None:
        self.context.close()
        self.browser.close()
        self.playwright.stop()
        self.assertEqual(self.errors, [])

    def respond(self, route: Route) -> None:
        from urllib.parse import urlsplit

        path = urlsplit(route.request.url).path
        method = route.request.method
        body = route.request.post_data_json if method == "POST" else None
        self.calls.append((path, method, body))
        if path == "/project-mode":
            route.fulfill(
                content_type="text/html",
                body='<html><body><script src="/locale.js"></script>'
                '<script src="/client.js"></script></body></html>',
            )
            return
        if path in {"/locale.js", "/client.js"}:
            filename = (
                "project_locale.js" if path == "/locale.js" else "project_mode.js"
            )
            route.fulfill(
                content_type="text/javascript",
                body=(
                    "window.atlasProjectProfile = " + json.dumps(self.profile()) + ";\n"
                    if path == "/client.js"
                    else ""
                )
                + (ROOT / "services/orchestrator" / filename).read_text(),
            )
            return
        value: Any = {"items": [], "next_cursor": None}
        if path == "/api/atlas/me":
            value = {"memberships": [{"org_id": ORG, "role": "member"}]}
        elif path == "/api/v1/auths/":
            value = {"id": "synthetic-browser-user"}
        elif path == f"/api/atlas/orgs/{ORG}/projects":
            value["items"] = [{"id": PROJECT, "name": "Example"}]
        elif path == BASE:
            value = {"id": PROJECT, "default_action": "chat"}
        elif path == BASE + "/conversations":
            value["items"] = [{"id": CONVERSATION, "title": "Document discussion"}]
        elif path.endswith("/turns"):
            value["items"] = self.turns
        elif path == BASE + "/files":
            value["items"] = [
                {
                    "id": VERSION,
                    "version_id": VERSION,
                    "folder_id": VERSION,
                    "relative_path": "Notes.md",
                    "locality": "server",
                    "index_status": "ready",
                    "availability": "retained",
                }
            ]
        elif path == BASE + "/instructions":
            value = {"version": 1, "text": "Keep originals"}
        elif path.endswith("/ledger/summary"):
            value = {
                "local": {"known": False},
                "server": {"files": 1, "bytes": 12},
                "model": {"calls": 0, "payload_bytes": 0, "unique_source_bytes": 0},
                "produced": {"files": 0, "bytes": 0},
                "as_of_seq": 0,
            }
        elif path.endswith("/context"):
            value = {
                "context_manifest": {
                    "coverage": {
                        "selected_units": 1,
                        "total_units": 1,
                        "unit": "paragraphs",
                        "complete": True,
                        "warnings": [],
                    }
                }
            }
        elif "/work-sessions" in path:
            value = {
                "id": SESSION,
                "conversation_id": CONVERSATION,
                "base_file_versions": [{"file_id": VERSION, "version_id": VERSION}],
                "state": "ready",
                "pending_approvals": self.approvals,
                "environment": {"workspace_id": SESSION},
                "effective_limits": {
                    "tool_calls": 10,
                    "wall_seconds": 300,
                    "cost_eur": 0.3,
                },
            }
        elif path.endswith("/decision"):
            value = {"decision": "denied"}
        elif path.endswith("/events"):
            after = int(urlsplit(route.request.url).query.split("=")[-1])
            route.fulfill(
                content_type="text/event-stream",
                body="".join(
                    "data: " + json.dumps(event) + "\n\n"
                    for event in self.events
                    if event["seq"] > after
                ),
            )
            return
        elif path == "/v1/chat/completions":
            assert isinstance(body, dict)
            self.turns = [
                {
                    "id": VERSION,
                    "request_id": SESSION,
                    "action": body["project_action"],
                    "state": "completed",
                    "content_refs": [
                        {"kind": "answer", "text": "Useful 59-site answer"}
                    ],
                }
            ]
            route.fulfill(
                content_type="text/event-stream",
                body='data: {"choices":[{"delta":{"content":"Useful 59-site answer"}}]}\r\n\r\ndata: [DONE]\r\n\r\n',
            )
            return
        route.fulfill(json=value)

    @staticmethod
    def profile() -> str:
        from packages.profiles import PROFILES

        return PROFILES[0]

    def test_client_receives_the_published_server_profile(self) -> None:
        from services.orchestrator.project_ui import project_asset

        response = project_asset("client.js")
        self.assertTrue(
            bytes(response.body)
            .decode()
            .startswith(
                "window.atlasProjectProfile = " + json.dumps(self.profile()) + ";"
            )
        )

    def mount(self, extra: str = "") -> None:
        self.page.goto(
            f"http://127.0.0.1:18790/project-mode?org={ORG}&project={PROJECT}&conversation={CONVERSATION}"
            + extra
        )
        self.page.locator("[data-send]:not([disabled])").wait_for()

    def test_explicit_scope_selection_and_enter_submit(self) -> None:
        self.mount()
        self.page.locator(".atlas-project-file input").check()
        self.page.get_by_role("textbox", name="Message", exact=True).fill(
            "Summarize both sources"
        )
        self.page.get_by_role("textbox", name="Message", exact=True).press("Enter")
        self.page.get_by_text("Useful 59-site answer", exact=True).wait_for()
        request = next(
            body for path, _, body in self.calls if path == "/v1/chat/completions"
        )
        self.assertEqual(request["file_version_ids"], [VERSION])
        self.assertEqual(
            (request["org_id"], request["project_id"], request["conversation_id"]),
            (ORG, PROJECT, CONVERSATION),
        )
        self.assertEqual(request["project_action"], "chat")
        from services.orchestrator.chat_schema import ChatRequest

        from services.retrieval.project_work_contract import validate

        validate("ProjectChatRequest", request)
        parsed = ChatRequest.model_validate(request)
        self.assertEqual(parsed.project_id, PROJECT)

    def test_work_reload_restores_selected_versions(self) -> None:
        self.mount("&session=" + SESSION)
        self.assertTrue(self.page.locator(".atlas-project-file input").is_checked())
        self.assertEqual(
            self.page.locator("[data-action=work]").get_attribute("aria-pressed"),
            "true",
        )
        self.page.reload()
        self.page.locator("[data-send]:not([disabled])").wait_for()
        self.assertTrue(self.page.locator(".atlas-project-file input").is_checked())
        self.assertFalse(
            any(
                method == "POST" and path.endswith("/work-sessions")
                for path, method, _ in self.calls
            )
        )

    def test_replay_deduplicates_and_filters_other_conversations(self) -> None:
        self.turns = [
            {
                "id": VERSION,
                "request_id": SESSION,
                "action": "chat",
                "state": "completed",
                "content_refs": [],
            }
        ]
        self.events = [
            dict(
                org_id=ORG,
                project_id=PROJECT,
                request_id=request,
                seq=seq,
                version=1,
                type="tool",
                text=text,
            )
            for seq, request, text in [
                (1, SESSION, "Owned event"),
                (2, VERSION, "Foreign event"),
            ]
        ]
        self.mount()
        self.page.get_by_text("Owned event", exact=True).wait_for()
        self.page.wait_for_timeout(1800)
        self.assertEqual(self.page.locator("[data-event-seq]").count(), 1)
        self.assertEqual(self.page.get_by_text("Foreign event", exact=True).count(), 0)
        self.page.reload()
        self.page.get_by_text("Owned event", exact=True).wait_for()
        self.assertEqual(self.page.locator("[data-event-seq]").count(), 1)

    def test_approval_is_explicit_digest_bound_and_single_use(self) -> None:
        approval = dict(
            id=VERSION,
            action_kind="delete",
            paths=["Notes.md"],
            destination={"kind": "server"},
            maximum_bytes=12,
            expires_at="2099-01-01T00:00:00Z",
            decision="pending",
            action_digest="synthetic-digest",
        )
        self.approvals = [approval]
        self.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="running",
                content_refs=[],
            )
        ]
        self.events = [
            dict(
                org_id=ORG,
                project_id=PROJECT,
                request_id=SESSION,
                seq=1,
                version=1,
                type="approval",
                output={"approval": approval},
            )
        ]
        self.mount("&session=" + SESSION)
        card = self.page.locator(".atlas-project-activity [data-approval]")
        self.assertTrue(
            card.get_by_role("button", name="Deny", exact=True).is_enabled()
        )
        card.get_by_role("button", name="Deny", exact=True).click()
        self.page.get_by_text(
            "Decision recorded; execution is separate", exact=True
        ).wait_for()
        self.assertFalse(
            card.get_by_role("button", name="Accept", exact=True).is_enabled()
        )
        decisions = [body for path, _, body in self.calls if path.endswith("/decision")]
        self.assertEqual(
            decisions, [{"allow": False, "action_digest": "synthetic-digest"}]
        )

    def test_file_preview_stays_scoped_and_local_inventory_unknown(self) -> None:
        self.mount()
        self.page.locator(".atlas-project-right").get_by_role(
            "button", name="Preview", exact=True
        ).click()
        preview = self.page.evaluate("window.preview")
        self.assertEqual(preview["kind"], "markdown")
        self.assertIn(
            BASE + "/files/" + VERSION + "/content?version=" + VERSION, preview["src"]
        )
        self.page.locator('[data-ledger-tab="local"]').click()
        self.assertIn("Unknown", self.page.get_by_role("tabpanel").inner_text())
        self.assertNotIn("Files: 0", self.page.get_by_role("tabpanel").inner_text())

    def test_all_locale_labels_are_complete(self) -> None:
        self.mount()
        locales = self.page.evaluate(
            "({keys:atlasProjectLocale.keys,rows:atlasProjectLocale.rows})"
        )
        self.assertEqual(set(locales["rows"]), {"en", "fr", "de", "es", "it"})
        for row in locales["rows"].values():
            self.assertEqual(len(row), len(locales["keys"]))
            self.assertTrue(all(row))
        for language, label in [
            ("fr", "Envoyer"),
            ("de", "Senden"),
            ("es", "Enviar"),
            ("it", "Invia"),
        ]:
            self.page.evaluate(
                "language => localStorage.setItem('locale', language)", language
            )
            self.page.evaluate(
                "document.getElementById('atlas-project').remove(); document.getElementById('atlas-project-entry').click()"
            )
            self.page.locator("[data-send]:not([disabled])").wait_for()
            self.assertEqual(self.page.locator("[data-send]").inner_text(), label)

    def test_decoded_response_from_old_scope_cannot_restore_content(self) -> None:
        self.mount()
        self.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="chat",
                state="completed",
                content_refs=[dict(kind="answer", text="Old scope private answer")],
            )
        ]
        self.page.evaluate("""() => {
          const original = window.fetch; let delay = true;
          window.fetch = async (...args) => {
            const response = await original(...args);
            if (delay && String(args[0]).endsWith('/turns')) {
              delay = false; const json = response.json.bind(response);
              response.json = async () => { const value = await json(); await new Promise(resolve => window.releaseOld = resolve); return value; };
            }
            return response;
          };
        }""")
        self.page.wait_for_function("Boolean(window.releaseOld)")
        self.page.get_by_label("Project", exact=True).select_option("")
        self.page.evaluate("window.releaseOld()")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator(".atlas-project-transcript").count(), 0)
        self.assertNotIn(
            "Old scope private answer", self.page.locator("body").inner_text()
        )

    def test_invalid_selector_fails_closed_without_chat_request(self) -> None:
        self.page.goto(f"http://127.0.0.1:18790/project-mode?org={ORG}&project=invalid")
        self.page.get_by_role("alert").wait_for()
        self.assertEqual(self.page.locator("[data-send]").count(), 0)
        self.assertFalse(
            any(path == "/v1/chat/completions" for path, _, _ in self.calls)
        )

    def test_expired_approval_stays_disabled(self) -> None:
        approval = dict(
            id=VERSION,
            action_kind="delete",
            paths=["Notes.md"],
            destination={"kind": "server"},
            maximum_bytes=12,
            expires_at="2000-01-01T00:00:00Z",
            decision="pending",
            action_digest="synthetic-digest",
        )
        self.approvals = [approval]
        self.mount("&session=" + SESSION)
        card = self.page.locator(".atlas-project-right [data-approval]")
        self.assertFalse(
            card.get_by_role("button", name="Accept", exact=True).is_enabled()
        )
        self.assertFalse(
            card.get_by_role("button", name="Deny", exact=True).is_enabled()
        )
        self.assertFalse(any(path.endswith("/decision") for path, _, _ in self.calls))

    def test_unknown_usage_is_not_presented_as_free(self) -> None:
        self.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="completed",
                content_refs=[],
                usage=dict(cost_eur=0, unknown_eur=0.3, reserved_eur=0.1),
            )
        ]
        self.mount()
        text = self.page.locator(".atlas-project-transcript").inner_text()
        self.assertIn("Unknown usage reservation EUR: 0.3", text)
        self.assertIn("Active reservation EUR: 0.1", text)
