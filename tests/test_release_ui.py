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
