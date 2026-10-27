"""REQ-PROJ-08 owner UI ruling, 2026-10-27: keyless browser regressions.

Synthetic HTTP fixtures exercise only the UI, not provider execution or storage.
The separate local-stack capture supplies pinned-host integration evidence.
"""

from __future__ import annotations

import unittest
from email.parser import BytesParser
from email import policy
from uuid import uuid4
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Route, expect

import project_composer_browser as fixture
from project_composer_browser import (
    BASE,
    CONVERSATION,
    ORG,
    PROJECT,
    ROOT,
    SESSION,
    VERSION,
)


class ProjectIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.locale = "fr-FR"
        self.project_items = [
            dict(
                id=f"20000000-0000-4000-8000-{index:012}",
                name=name,
                updated_at="2026-10-26T12:00:00Z",
                revision=1,
            )
            for index, name in enumerate(
                ["Annual report", "Website", "Budget 2027", "Team guide"], 1
            )
        ]
        self.folder_revision = 0
        self.file_items: list[dict[str, Any]] = [
            dict(
                id=VERSION,
                version_id=VERSION,
                folder_id=VERSION,
                relative_path="Notes.md",
                locality="server",
                index_status="ready",
                availability="retained",
            )
        ]
        self.memory: list[dict[str, Any]] = []
        self.deliverables: list[dict[str, Any]] = []
        self.fixture = fixture.ProjectComposerTests()
        self.fixture.setUp()
        self.context = self.fixture.context
        self.page = self.fixture.page
        self.calls = self.fixture.calls
        self.turns = self.fixture.turns
        self.events = self.fixture.events
        self.approvals = self.fixture.approvals
        self.context.unroute("**/*")
        self.context.route("**/*", self.respond)
        self.context.add_init_script(
            "localStorage.removeItem('locale');"
            "Object.defineProperty(navigator,'language',{value:'fr-FR'});"
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def mount(self, extra: str = "") -> None:
        self.fixture.mount(extra)

    def respond(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path in {"/", "/admin/settings"}:
            route.fulfill(
                content_type="text/html",
                body="""<!doctype html><html lang="en"><head>
                <link rel="stylesheet" href="/project-mode/style.css">
                <style>body{margin:0}#sidebar{position:fixed;width:250px;height:100vh}
                #chat-container{margin-left:250px;height:100vh}</style>
                <script defer src="/locale.js"></script><script defer src="/client.js"></script>
                </head><body><div style="display:contents"><nav id="sidebar">
                <a href="/">New chat</a><a href="/notes">Notes</a></nav>
                <main id="chat-container"><textarea id="chat-input"></textarea></main>
                </div></body></html>""",
            )
            return
        if path == "/static/atlas/vendor/marked/marked.esm.js":
            route.fulfill(
                content_type="text/javascript",
                body=(
                    ROOT / "services/orchestrator/static/vendor/marked/marked.esm.js"
                ).read_text(),
            )
            return
        if path == "/project-mode/style.css":
            route.fulfill(
                content_type="text/css",
                body=(ROOT / "services/orchestrator/project_mode.css").read_text(),
            )
            return
        if path == f"/api/atlas/orgs/{ORG}/projects":
            if route.request.method == "POST":
                payload = route.request.post_data_json
                assert isinstance(payload, dict)
                value = dict(self.project_items[0], **payload)
                self.project_items.append(value)
                route.fulfill(json=value)
            else:
                route.fulfill(json={"items": self.project_items, "next_cursor": None})
            return
        if path == BASE + "/folders":
            route.fulfill(
                json={
                    "items": [
                        {
                            "id": VERSION,
                            "name": "Sources",
                            "locality": "server",
                            "model_use": "allow",
                            "revision": self.folder_revision,
                        }
                    ],
                    "next_cursor": None,
                }
            )
            return
        if path == BASE + "/files":
            if route.request.method == "POST":
                self.calls.append((path, "POST", None))
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: "
                    + route.request.headers["content-type"].encode()
                    + b"\r\n\r\n"
                    + (route.request.post_data_buffer or b"")
                )
                parts = {
                    part.get_param("name", header="content-disposition"): part
                    for part in message.iter_parts()
                }
                revision_bytes = parts["expected_parent_revision"].get_payload(
                    decode=True
                )
                assert isinstance(revision_bytes, bytes)
                revision = int(revision_bytes)
                self.assertEqual(revision, self.folder_revision)
                name_bytes = parts["path"].get_payload(decode=True)
                assert isinstance(name_bytes, bytes)
                name = name_bytes.decode()
                file = dict(
                    id=str(uuid4()),
                    version_id=str(uuid4()),
                    folder_id=VERSION,
                    relative_path=name,
                    locality="server",
                    index_status="ready",
                    availability="retained",
                )
                self.file_items.append(file)
                self.folder_revision += 1
                route.fulfill(json={"file": file, "job_id": str(uuid4())})
            else:
                route.fulfill(json={"items": self.file_items, "next_cursor": None})
            return
        if path == BASE:
            value = self.project_items[0]
            if route.request.method == "PATCH":
                payload = route.request.post_data_json
                assert isinstance(payload, dict)
                self.calls.append((path, "PATCH", payload))
                value.update(payload)
            route.fulfill(json=value)
            return
        if path == BASE + "/conversations" and route.request.method == "POST":
            self.calls.append((path, "POST", route.request.post_data_json))
            route.fulfill(json={"id": CONVERSATION})
            return
        if path in {BASE + "/memory", BASE + "/deliverables"}:
            route.fulfill(
                json={
                    "items": self.memory
                    if path.endswith("memory")
                    else self.deliverables,
                    "next_cursor": None,
                }
            )
            return
        self.fixture.turns = self.turns
        self.fixture.events = self.events
        self.fixture.approvals = self.approvals
        self.fixture.respond(route)
        self.turns = self.fixture.turns

    def open_projects(self) -> None:
        self.page.goto("http://127.0.0.1:18790/")
        self.page.locator("#atlas-project-entry").click()
        expect(self.page.locator("[data-project-card]")).to_have_count(4)

    def test_native_list_search_tabs_sort_and_project_overview(self) -> None:
        self.open_projects()
        self.assertTrue(self.page.locator("#sidebar #atlas-project-entry").is_visible())
        self.assertFalse(self.page.locator('#sidebar a[href="/notes"]').is_visible())
        expect(self.page.get_by_role("tab", name="Organisation")).to_have_count(0)
        self.page.get_by_role("tab", name="Partagé avec vous").click()
        expect(
            self.page.get_by_text("Aucun projet partagé pour le moment")
        ).to_be_visible()
        self.page.get_by_role("tab", name="Vos projets").click()
        self.page.get_by_role("button", name="Rechercher un projet", exact=True).click()
        self.page.get_by_role("searchbox").fill("budget")
        expect(self.page.locator("[data-project-card]")).to_have_count(1)
        self.page.get_by_role("searchbox").fill("")
        self.page.get_by_label("Trier les projets").select_option("name")
        expect(self.page.locator("[data-project-card]").first).to_contain_text(
            "Annual report"
        )
        self.page.locator(f'[data-project-card="{PROJECT}"]').click()
        expect(self.page.locator('[data-view="project"]')).to_be_visible()
        expect(self.page.locator(".atlas-project-conversations")).to_contain_text(
            "Document discussion"
        )
        expect(self.page.locator('[data-panel="instructions"]')).to_contain_text(
            "Instructions"
        )
        expect(self.page.locator('[data-panel="memory"]')).to_have_count(0)
        expect(self.page.locator("[data-action]")).to_have_count(0)
        self.assertNotIn("conversation=", self.page.url)
        self.assertFalse(self.page.locator("#sidebar").get_attribute("inert"))
        self.page.locator("[data-conversation]").click()
        expect(self.page.locator('[data-view="conversation"]')).to_be_visible()
        self.page.get_by_role("button", name="Annual report", exact=True).click()
        expect(self.page.locator('[data-view="project"]')).to_be_visible()

    def test_project_start_conversation_and_scope(self) -> None:
        self.open_projects()
        self.page.locator(f'[data-project-card="{PROJECT}"]').click()
        self.page.get_by_role("textbox", name="Message", exact=True).fill(
            "Summarize the notes"
        )
        self.page.locator("[data-send]").click()
        expect(self.page.locator(".atlas-project-transcript")).to_contain_text(
            "Useful 59-site answer"
        )
        body = next(
            body for path, _, body in self.calls if path == "/v1/chat/completions"
        )
        from services.retrieval.project_work_contract import validate

        validate("ProjectChatRequest", body)
        self.assertEqual(body["project_id"], PROJECT)
        self.assertEqual(body["org_id"], ORG)
        self.assertEqual(body["conversation_id"], CONVERSATION)
        self.assertEqual(body["file_version_ids"], [VERSION])
        self.assertEqual(
            len(
                [
                    p
                    for p, method, _ in self.calls
                    if p == BASE + "/conversations" and method == "POST"
                ]
            ),
            1,
        )

    def test_rename_with_revision_and_no_html_injection(self) -> None:
        self.project_items[0]["name"] = '<img src=x onerror="alert(1)">'
        self.open_projects()
        expect(self.page.locator(".atlas-project-card img")).to_have_count(0)
        self.page.locator(f'[data-project-card="{PROJECT}"]').click()
        self.page.get_by_role("button", name="Renommer le projet").click()
        self.page.get_by_role("textbox", name="Nom", exact=True).fill("Revised report")
        self.page.get_by_role("button", name="Enregistrer", exact=True).click()
        expect(self.page.get_by_role("heading", name="Revised report")).to_be_visible()

    def test_file_cards_scoped_download_and_preview(self) -> None:
        self.deliverables = [
            dict(
                id=VERSION,
                file_version_id=VERSION,
                filename="Report.docx",
                download_route=BASE + "/deliverables/" + VERSION + "/download",
                request_id=SESSION,
            )
        ]
        self.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="completed",
                content_refs=[dict(kind="answer", text="Your document is ready.")],
            )
        ]
        self.mount()
        card = self.page.locator(".atlas-project-transcript [data-deliverable]")
        expect(card).to_be_visible()
        download = card.get_by_role("link", name="Télécharger").get_attribute("href")
        assert download is not None
        self.assertTrue(download.endswith("/download"))
        card.get_by_role("button", name="Aperçu").click()
        self.page.wait_for_function("Boolean(window.preview)")
        self.assertEqual(self.page.evaluate("window.preview.kind"), "markdown")

    def test_mobile_grid_and_theme_and_locale_completeness(self) -> None:
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.open_projects()
        cards = self.page.locator("[data-project-card]")
        first, second = cards.nth(0).bounding_box(), cards.nth(1).bounding_box()
        assert first is not None and second is not None
        self.assertEqual(first["x"], second["x"])
        self.assertGreater(second["y"], first["y"])
        self.page.locator(f'[data-project-card="{PROJECT}"]').click()
        expect(self.page.locator('[data-view="project"]')).to_be_visible()
        right, center = (
            self.page.locator(".atlas-project-right").bounding_box(),
            self.page.locator(".atlas-project-center").bounding_box(),
        )
        assert right is not None and center is not None
        self.assertGreaterEqual(right["y"], center["y"] + center["height"] - 1)
        self.page.evaluate("document.documentElement.className='dark'")
        self.assertEqual(
            self.page.locator("#atlas-project").evaluate(
                "n => getComputedStyle(n).backgroundColor"
            ),
            "rgb(36, 36, 34)",
        )
        self.assertTrue(
            self.page.evaluate(
                "Object.values(atlasProjectLocale.rows).every(row => row.length === atlasProjectLocale.keys.length && row.every(Boolean))"
            )
        )
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"), 390
        )

    def test_approval_decision_and_expiry_are_preserved(self) -> None:
        self.approvals = [
            dict(
                id=VERSION,
                action_kind="delete",
                paths=["Notes.md"],
                destination={"kind": "server"},
                maximum_bytes=40,
                expires_at="2099-01-01T00:00:00Z",
                decision="pending",
                action_digest="a" * 64,
            )
        ]
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
                conversation_id=CONVERSATION,
                request_id=SESSION,
                version=1,
                seq=1,
                type="approval",
                status="awaiting_approval",
                output={"approval": self.approvals[0]},
            )
        ]
        self.mount("&session=" + SESSION)
        card = self.page.locator("[data-approval]")
        card.get_by_role("button", name="Refuser", exact=True).click()
        expect(card.get_by_role("button", name="Refuser", exact=True)).to_be_disabled()
        decisions = [
            body
            for path, method, body in self.calls
            if path.endswith("/decision") and method == "POST"
        ]
        self.assertEqual(decisions, [{"allow": False, "action_digest": "a" * 64}])
        self.approvals[0]["expires_at"] = "2000-01-01T00:00:00Z"
        self.page.reload()
        expect(
            self.page.locator("[data-approval]").get_by_role(
                "button", name="Accepter", exact=True
            )
        ).to_be_disabled()

    def test_instruction_change_uses_scoped_conversation_not_direct_write(self) -> None:
        self.open_projects()
        self.page.locator(f'[data-project-card="{PROJECT}"]').click()
        self.page.get_by_role("textbox", name="Instructions", exact=True).fill(
            "Use concise language."
        )
        self.page.get_by_role(
            "button", name="Demander la modification", exact=True
        ).click()
        expect(self.page.locator(".atlas-project-transcript")).to_contain_text(
            "Useful 59-site answer"
        )
        body = next(
            body for path, _, body in self.calls if path == "/v1/chat/completions"
        )
        self.assertEqual(body["project_action"], "work")
        self.assertEqual(body["work_session_id"], SESSION)
        self.assertIn("Use concise language.", body["messages"][0]["content"])
        self.assertFalse(any(method == "PUT" for _, method, _ in self.calls))

    def test_all_locales_and_explicit_choice_override_browser(self) -> None:
        for language, label in [
            ("fr", "Vos projets"),
            ("en", "Your projects"),
            ("de", "Ihre Projekte"),
            ("es", "Tus proyectos"),
            ("it", "I tuoi progetti"),
        ]:
            self.context.add_init_script(
                "localStorage.setItem('locale','" + language + "');"
            )
            self.open_projects()
            expect(self.page.get_by_role("tab", name=label, exact=True)).to_be_visible()

    def test_session_scope_mismatch_fails_closed(self) -> None:
        self.context.route(
            "**/work-sessions/" + SESSION,
            lambda route: route.fulfill(
                json={
                    "id": SESSION,
                    "conversation_id": VERSION,
                    "base_file_versions": [],
                    "pending_approvals": [],
                    "state": "ready",
                }
            ),
        )
        self.page.goto(
            f"http://127.0.0.1:18790/project-mode?org={ORG}&project={PROJECT}&conversation={CONVERSATION}&session={SESSION}"
        )
        expect(self.page.locator("#atlas-project-failure")).to_be_visible()
        expect(self.page.locator("[data-send]")).to_have_count(0)
        self.assertFalse(
            any(path == "/v1/chat/completions" for path, _, _ in self.calls)
        )

    def test_remote_targets_are_only_in_admin_settings(self) -> None:
        self.open_projects()
        expect(
            self.page.get_by_role("button", name="Cibles distantes", exact=True)
        ).to_have_count(0)
        self.context.route(
            "**/api/atlas/me",
            lambda route: route.fulfill(
                json={
                    "memberships": [
                        {"org_id": ORG, "role": "admin", "name": "Test organization"}
                    ]
                }
            ),
        )
        self.page.goto("http://127.0.0.1:18790/admin/settings")
        self.page.get_by_role("button", name="Cibles distantes", exact=True).click()
        expect(
            self.page.get_by_role("heading", name="Exécution à distance", exact=True)
        ).to_be_visible()
        expect(
            self.page.get_by_role("button", name="Enregistrer la cible", exact=True)
        ).to_be_visible()

    def test_pending_approval_restored_from_conversation_events(self) -> None:
        self.approvals = [
            dict(
                id=VERSION,
                action_kind="delete",
                paths=["Notes.md"],
                destination={"kind": "server"},
                maximum_bytes=40,
                expires_at="2099-01-01T00:00:00Z",
                decision="pending",
                action_digest="a" * 64,
            )
        ]
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
                conversation_id=CONVERSATION,
                session_id=SESSION,
                request_id=SESSION,
                version=1,
                seq=1,
                type="approval",
                status="awaiting_approval",
                output={"approval": self.approvals[0]},
            )
        ]
        self.mount()
        expect(
            self.page.locator("[data-approval]").get_by_role(
                "button", name="Accepter", exact=True
            )
        ).to_be_enabled()
        self.assertIn("session=" + SESSION, self.page.url)

    def test_document_preview_is_read_only_and_localized(self) -> None:
        self.context.route(
            "**/preview",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body='<html><body><div style="display:contents"></div><script src="/preview-client.js"></script></body></html>',
            ),
        )
        self.context.route(
            "**/preview-client.js",
            lambda route: route.fulfill(
                content_type="text/javascript; charset=utf-8",
                # Clear the base fixture's explicit English setting after all
                # init scripts, before the real preview creates its controls.
                body="localStorage.removeItem('locale');delete window.atlasSideView;"
                + (ROOT / "services/orchestrator/chat-ui.js").read_text(),
            ),
        )
        self.context.route(
            "**/static/atlas/vendor/marked/marked.esm.js",
            lambda route: route.fulfill(
                content_type="text/javascript",
                body=(
                    ROOT / "services/orchestrator/static/vendor/marked/marked.esm.js"
                ).read_text(),
            ),
        )
        self.page.goto("http://127.0.0.1:18790/preview")
        self.page.wait_for_function("Boolean(window.atlasSideView)")
        self.page.evaluate(
            "atlasSideView.open({kind:'markdown',content:'# Annual report\\n\\nReview the budget.',title:'Report.docx',readOnly:true})"
        )
        expect(self.page.locator(".atlas-side-preview")).to_contain_text(
            "Annual report"
        )
        expect(self.page.locator(".atlas-side-editor")).to_be_hidden()
        expect(
            self.page.get_by_role("button", name="Modifier", exact=True)
        ).to_have_count(0)
        self.page.get_by_role(
            "button", name="Fermer la vue latérale", exact=True
        ).click()
        expect(self.page.locator("#atlas-side-view")).to_be_hidden()

    def test_next_turn_refreshes_changed_file_snapshot(self) -> None:
        self.mount()
        message = self.page.get_by_role("textbox", name="Message", exact=True)
        message.fill("Summarize the source")
        self.page.locator("[data-send]").click()
        expect(self.page.locator(".atlas-project-transcript")).to_contain_text(
            "Useful 59-site answer"
        )
        expect(self.page.locator("[data-send]")).to_be_enabled()
        updated = "50000000-0000-4000-8000-000000000002"
        self.context.route(
            "**" + BASE + "/files",
            lambda route: route.fulfill(
                json={
                    "items": [
                        {
                            "id": VERSION,
                            "version_id": updated,
                            "folder_id": VERSION,
                            "relative_path": "Notes.md",
                            "locality": "server",
                            "availability": "retained",
                            "index_status": "ready",
                        }
                    ],
                    "next_cursor": None,
                }
            ),
        )
        # Reopening uses the saved session but must acquire the changed source version.
        self.page.reload()
        self.page.locator("[data-send]:not([disabled])").wait_for()
        self.page.get_by_role("textbox", name="Message", exact=True).fill(
            "Review the updated source"
        )
        self.page.locator("[data-send]").click()
        expect(self.page.locator("[data-send]")).to_be_enabled()
        calls = [
            body
            for path, method, body in self.calls
            if path == "/v1/chat/completions" and method == "POST"
        ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["file_version_ids"], [updated])
        sessions = [
            body
            for path, method, body in self.calls
            if path == BASE + "/work-sessions" and method == "POST"
        ]
        self.assertEqual(
            sessions[-1]["base_versions"], [{"file_id": VERSION, "version_id": updated}]
        )

    def test_answer_markdown_is_readable_and_scoped(self) -> None:
        self.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="chat",
                state="completed",
                content_refs=[
                    dict(
                        kind="answer",
                        text="## Review\n\n- First item\n- Second item\n\n**Total**: 42\n\n[Untrusted](https://example.invalid/)\n\n<img src=x onerror=alert(1)><script>alert(1)</script>",
                    )
                ],
            )
        ]
        self.mount()
        answer = self.page.locator('.answer[data-format="markdown"]')
        expect(answer.get_by_role("heading", name="Review", exact=True)).to_be_visible()
        expect(answer.locator("li")).to_have_count(2)
        expect(answer.locator("strong")).to_have_text("Total")
        expect(answer.locator("img,script,a")).to_have_count(0)

    def test_drop_two_files_without_starting_a_model_turn(self) -> None:
        self.open_projects()
        self.page.locator(f'[data-project-card="{PROJECT}"]').click()
        drop = self.page.locator(".atlas-project-drop")
        drop.wait_for()
        transfer = self.page.evaluate_handle(
            "() => {const data=new DataTransfer(); data.items.add(new File(['# Brief'],'Brief.md',{type:'text/markdown'})); data.items.add(new File(['Budget: 12000'],'Budget.txt',{type:'text/plain'})); return data;}"
        )
        drop.dispatch_event("drop", {"dataTransfer": transfer})
        expect(self.page.locator('[data-panel="files"]')).to_contain_text("Brief.md")
        expect(self.page.locator('[data-panel="files"]')).to_contain_text("Budget.txt")
        expect(self.page.locator("[data-send]")).to_be_enabled()
        self.assertEqual(self.folder_revision, 2)
        self.assertFalse(
            any(path == "/v1/chat/completions" for path, _, _ in self.calls)
        )
