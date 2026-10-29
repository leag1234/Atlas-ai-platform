"""Owner release UI defects exercised in the real DOM with synthetic scoped data."""

import unittest
from playwright.sync_api import expect
import project_integration_browser as fixture


class ReleaseUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture.ProjectIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_search_is_visible_and_focus_only_surrounds_composer(self) -> None:
        f = self.fixture
        f.open_projects()
        expect(f.page.get_by_role("searchbox")).to_be_visible()
        bounds = f.page.get_by_role(
            "button", name="Rechercher un projet", exact=True
        ).bounding_box()
        assert bounds is not None
        self.assertGreaterEqual(bounds["width"], 40)
        f.page.locator("[data-project-card]").first.click()
        text = f.page.get_by_role("textbox", name="Message", exact=True)
        text.focus()
        self.assertEqual(text.evaluate("n=>getComputedStyle(n).outlineStyle"), "none")
        self.assertEqual(
            f.page.locator(".atlas-project-composer").evaluate(
                "n=>getComputedStyle(n).outlineStyle"
            ),
            "solid",
        )

    def test_work_profile_and_french_locale_reach_api(self) -> None:
        f = self.fixture
        from services.orchestrator.project_ui import project_asset

        f.context.route(
            "**/client.js",
            lambda route: route.fulfill(
                content_type="text/javascript; charset=utf-8",
                body=bytes(project_asset("client.js").body).decode(),
            ),
        )
        f.open_projects()
        f.page.locator("[data-project-card]").first.click()
        f.page.get_by_role("textbox", name="Message", exact=True).fill(
            "Prépare une synthèse"
        )
        with f.page.expect_request("**/v1/chat/completions") as sent:
            f.page.locator("[data-send]").click()
        body = sent.value.post_data_json
        assert isinstance(body, dict)
        from services.orchestrator.code_mode import code_profile

        self.assertEqual(body["model"], code_profile())
        self.assertEqual(body["project_action"], "work")
        self.assertEqual(body["ui_locale"], "fr")

    def test_deliverable_has_one_download_control_in_answer(self) -> None:
        from project_composer_browser import BASE, SESSION, VERSION

        f = self.fixture
        route = BASE + "/deliverables/" + VERSION + "/download"
        f.file_items[0]["relative_path"] = "internal-folder/Synthèse.docx"
        f.deliverables = [
            dict(
                id=VERSION,
                filename="internal-folder/Synthèse.docx",
                file_version_id=VERSION,
                request_id=SESSION,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                download_route=route,
                validation={"status": "passed"},
            )
        ]
        f.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="completed",
                content_refs=[
                    dict(
                        kind="answer",
                        text="Voici la synthèse. [Télécharger la synthèse]("
                        + route
                        + ")",
                    )
                ],
            )
        ]
        f.mount()
        expect(
            f.page.locator(".atlas-project-transcript [data-deliverable]")
        ).to_have_count(1)
        expect(
            f.page.locator(".atlas-project-transcript [data-deliverable] strong")
        ).to_have_text("Synthèse.docx")
        expect(f.page.locator(".atlas-project-transcript .answer a")).to_have_count(0)
        expect(
            f.page.locator(".atlas-project-transcript [data-deliverable] a")
        ).to_have_count(1)

    def test_brand_uses_authenticated_name_in_sidebar(self) -> None:
        from pathlib import Path

        f = self.fixture
        f.context.route(
            "**/api/v1/auths/",
            lambda route: route.fulfill(json={"id": "synthetic", "name": "Camille"}),
        )
        f.open_projects()
        f.page.locator("#sidebar").evaluate(
            "n=>{const b=document.createElement('button'); b.textContent='User'; b.id='account'; n.append(b);}"
        )
        f.page.add_script_tag(
            content=Path("services/orchestrator/brand-ui.js").read_text()
        )
        expect(f.page.locator("#account")).to_have_text("Camille")

    def test_replayed_publication_renders_one_approval_card(self) -> None:
        from project_composer_browser import (
            ORG,
            PROJECT,
            CONVERSATION,
            SESSION,
            VERSION,
        )

        f = self.fixture
        approval = dict(
            id=VERSION,
            action_kind="publish_change",
            paths=["totals.py"],
            destination={"kind": "server"},
            maximum_bytes=40,
            expires_at="2099-01-01T00:00:00Z",
            decision="pending",
            action_digest="a" * 64,
        )
        f.approvals.append(approval)
        f.turns.append(
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="running",
                content_refs=[],
            )
        )
        for seq in (1, 2):
            f.events.append(
                dict(
                    org_id=ORG,
                    project_id=PROJECT,
                    conversation_id=CONVERSATION,
                    request_id=SESSION,
                    version=1,
                    seq=seq,
                    type="approval",
                    status="awaiting_approval",
                    output={"approval": approval},
                )
            )
        f.mount("&session=" + SESSION)
        expect(f.page.locator("[data-approval]")).to_have_count(1)

    def test_finished_answer_has_one_closed_step_group_and_no_diagnostics(self) -> None:
        from project_composer_browser import ORG, PROJECT, CONVERSATION, SESSION

        f = self.fixture
        f.turns.append(
            dict(
                id="turn",
                request_id=SESSION,
                action="work",
                state="completed",
                content_refs=[dict(kind="answer", text="Le fichier est prêt.")],
                usage=dict(cost_eur=0, unknown_eur=0.01, reserved_eur=0.02),
            )
        )
        for seq, status in enumerate(("running", "completed", "running"), 1):
            f.events.append(
                dict(
                    org_id=ORG,
                    project_id=PROJECT,
                    conversation_id=CONVERSATION,
                    request_id=SESSION,
                    version=1,
                    seq=seq,
                    type="tool",
                    status=status,
                    output=dict(verification=dict(status="not_run"), cost_eur=0),
                )
            )
        f.mount()
        group = f.page.locator(".atlas-project-turn details[data-steps]")
        expect(group).to_have_count(1)
        expect(group.locator("summary")).to_have_text("Étapes (3)")
        self.assertFalse(group.evaluate("n=>n.open"))
        body = f.page.locator("#atlas-project").inner_text()
        for diagnostic in ("Vérification", "Coût EUR", "Réserve pour", "En cours"):
            self.assertNotIn(diagnostic, body)
        group.locator("summary").click()
        self.assertNotIn("En cours", group.inner_text())

    def test_long_titles_and_file_names_keep_whole_words(self) -> None:
        from project_composer_browser import BASE, CONVERSATION

        f = self.fixture
        title = "Prepare a detailed synthesis of the quarterly expenses and the budget by category"
        f.context.route(
            "**" + BASE + "/conversations",
            lambda route: route.fulfill(
                json=dict(items=[dict(id=CONVERSATION, title=title)], next_cursor=None)
            ),
        )
        filename = "Note_Synthese_Quartz.docx"
        f.file_items[0]["relative_path"] = filename
        f.open_projects()
        f.page.locator("[data-project-card]").first.click()
        label = f.page.locator(".atlas-project-conversation span").inner_text()
        self.assertLessEqual(len(label), 61)
        self.assertTrue(label.endswith("…"))
        self.assertIn(label[:-1], title)
        self.assertEqual(title[len(label) - 1], " ")
        file = f.page.locator(".atlas-project-file > button").first
        expect(file).to_have_attribute("title", filename)
        self.assertEqual(file.evaluate("n=>getComputedStyle(n).whiteSpace"), "nowrap")
        self.assertEqual(
            file.evaluate("n=>getComputedStyle(n).textOverflow"), "ellipsis"
        )
        remove = f.page.locator(".atlas-project-file [data-remove]")
        expect(remove).to_have_count(1)
        self.assertEqual(remove.evaluate("n=>getComputedStyle(n).opacity"), "0")
        f.page.locator(".atlas-project-file").hover()
        self.assertEqual(remove.evaluate("n=>getComputedStyle(n).opacity"), "1")

    def test_project_load_error_replaces_loader_after_navigation(self) -> None:
        from project_composer_browser import ORG

        f = self.fixture
        f.open_projects()
        f.page.locator("[data-project-card]").first.click()
        expect(f.page.locator("[data-send]")).to_be_enabled()
        f.context.route(
            "**/api/atlas/orgs/" + ORG + "/projects",
            lambda route: route.fulfill(status=503, json=dict(error="internal_error")),
        )
        f.page.locator(".atlas-project-breadcrumb").first.click()
        expect(f.page.locator(".atlas-project-notice")).not_to_have_text("Chargement…")
        expect(f.page.locator(".atlas-project-notice")).to_contain_text("service")
        self.assertNotIn(
            "internal_error", f.page.locator("#atlas-project").inner_text()
        )

    def test_direct_phone_route_has_one_navigation_header(self) -> None:
        from pathlib import Path

        f = self.fixture
        f.page.set_viewport_size({"width": 390, "height": 844})
        f.mount()
        f.page.add_style_tag(
            content=Path("services/orchestrator/project_mode.css").read_text()
        )
        expect(f.page.locator(".atlas-project-mobile-header")).to_be_visible()
        expect(f.page.locator(".atlas-project-left")).to_be_hidden()
        self.assertEqual(
            f.page.locator('#atlas-project img[alt="Atlas"]:visible').count(), 1
        )

    def test_deliverable_label_retains_space_next_to_download_controls(self) -> None:
        from pathlib import Path
        from project_composer_browser import BASE, SESSION, VERSION

        f = self.fixture
        filename = "Note_Synthese_Quartz.docx"
        f.file_items[0]["relative_path"] = filename
        f.deliverables = [
            dict(
                id=VERSION,
                filename=filename,
                file_version_id=VERSION,
                request_id=SESSION,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                download_route=BASE + "/deliverables/" + VERSION + "/download",
                validation={"status": "passed"},
            )
        ]
        f.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="completed",
                content_refs=[dict(kind="answer", text="Voici la synthèse.")],
            )
        ]
        f.page.set_viewport_size({"width": 1440, "height": 1000})
        f.mount()
        f.page.add_style_tag(
            content=Path("services/orchestrator/project_mode.css").read_text()
        )
        for selector, width, height in (
            (".atlas-project-right", 1440, 1000),
            (".atlas-project-transcript", 390, 844),
        ):
            with self.subTest(selector=selector):
                f.page.set_viewport_size({"width": width, "height": height})
                label = f.page.locator(selector + " [data-deliverable] strong")
                expect(label).to_have_text(filename)
                expect(label).to_have_attribute("title", filename)
                bounds = label.bounding_box()
                assert bounds is not None
                self.assertGreaterEqual(bounds["width"], 80)
