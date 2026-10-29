"""Browser checks for the release 0.7 fifth-pass presentation fixes (optional runner)."""

from pathlib import Path
import unittest


class PresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        import project_integration_browser as fixture

        self.fixture = fixture.ProjectIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_file_card_is_only_file_description_and_compact(self) -> None:
        from playwright.sync_api import expect
        from project_composer_browser import BASE, SESSION, VERSION

        f = self.fixture
        route = BASE + "/deliverables/" + VERSION + "/download"
        f.deliverables = [
            dict(
                id=VERSION,
                filename="Synthese.docx",
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
                        text="Voici la synthèse.\nLien de téléchargement : [Télécharger]("
                        + route
                        + ")\nLien de prévisualisation : [Voir]("
                        + route.replace("/download", "/preview")
                        + ")\nFichier produit : Synthese.docx (37 547 octets)",
                    )
                ],
            )
        ]
        f.mount()
        f.page.add_style_tag(
            content=Path("services/orchestrator/project_mode.css").read_text()
        )
        answer = f.page.locator(".atlas-project-transcript .answer")
        expect(answer).to_have_text("Voici la synthèse.")
        card = f.page.locator(".atlas-project-right [data-deliverable]").first
        expect(card).to_be_visible()
        bounds = card.bounding_box()
        assert bounds is not None
        self.assertLess(bounds["height"], 145)
        self.assertNotEqual(
            card.locator("a").evaluate("n=>getComputedStyle(n).color"), "rgb(0, 0, 238)"
        )

    def test_live_word_empty_format_label_is_removed(self) -> None:
        from playwright.sync_api import expect
        from project_composer_browser import BASE, SESSION, VERSION

        f = self.fixture
        route = BASE + "/deliverables/" + VERSION + "/download"
        f.deliverables = [
            dict(
                id=VERSION,
                filename="Synthese.docx",
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
                        text="La note est prête.\n**Fichier DOCX** : [Télécharger]("
                        + route
                        + ") · [Aperçu]("
                        + route.replace("/download", "/preview")
                        + ")",
                    )
                ],
            )
        ]
        f.mount()
        expect(f.page.locator(".atlas-project-transcript .answer")).to_have_text(
            "La note est prête."
        )

    def test_live_file_heading_bad_scope_links_and_chunk_are_removed(self) -> None:
        from playwright.sync_api import expect
        from project_composer_browser import BASE, SESSION, VERSION

        f = self.fixture
        route = BASE + "/deliverables/" + VERSION + "/download"
        f.deliverables = [
            dict(
                id=VERSION,
                filename="Depenses_Totaux_v3.xlsx",
                file_version_id=VERSION,
                request_id=SESSION,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
                        text="## Fichier produit\n**`Depenses_Totaux_v3.xlsx`** — "
                        + "[Télécharger](/wrong-project/deliverables/"
                        + VERSION
                        + "/download) · [Aperçu](/wrong-project/deliverables/"
                        + VERSION
                        + "/preview)\nTotaux (chunk `"
                        + "d8bd5878272ab92f67870d89f059e2cef834dab75afafb9ffdb1b766edb726b1"
                        + "` : Formation = 500).",
                    )
                ],
            )
        ]
        f.mount()
        expect(f.page.locator(".atlas-project-transcript .answer")).to_have_text(
            "Totaux (Formation = 500)."
        )

    def test_live_validation_diagnostics_are_not_reply_text(self) -> None:
        from playwright.sync_api import expect
        from project_composer_browser import SESSION, VERSION
        from services.orchestrator.project_transcript import answer_content

        text = (
            "Le total est de 1500 euros.\n"
            "La validation du livrable est passée (`package_readable`, "
            "`requested_content`, `source_coverage`)."
        )
        f = self.fixture
        f.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                action="work",
                state="completed",
                content_refs=[dict(kind="answer", text=text)],
            )
        ]
        f.mount()
        expect(f.page.locator(".atlas-project-transcript .answer")).to_have_text(
            "Le total est de 1500 euros."
        )
        self.assertEqual(answer_content(text), "Le total est de 1500 euros.")

    def test_native_sidebar_resize_keeps_project_content_beside_it(self) -> None:
        from playwright.sync_api import expect

        f = self.fixture
        f.open_projects()
        root = f.page.locator("#atlas-project")
        expect(root).to_have_attribute("data-owned", "false")
        f.page.locator("#chat-container").evaluate(
            "n=>{n.style.transition='margin-left 120ms linear';n.style.marginLeft='320px';}"
        )
        expect(root).to_have_css("left", "320px")
        f.page.locator("#chat-container").evaluate("n=>n.style.marginLeft='70px'")
        expect(root).to_have_css("left", "70px")

    def test_project_card_has_instruction_excerpt_and_counts(self) -> None:
        from playwright.sync_api import expect

        f = self.fixture
        project = str(f.project_items[0]["id"])
        f.context.route(
            "**/projects/" + project + "/instructions",
            lambda route: route.fulfill(
                json={"text": "Budget et calendrier du projet\nPrivate second line"}
            ),
        )
        f.context.route(
            "**/projects/" + project + "/files",
            lambda route: route.fulfill(json={"items": [{}, {}], "next_cursor": None}),
        )
        f.context.route(
            "**/projects/" + project + "/conversations",
            lambda route: route.fulfill(json={"items": [{}], "next_cursor": None}),
        )
        f.open_projects()
        card = f.page.locator("[data-project-card]").first
        expect(card).to_contain_text("Budget et calendrier du projet")
        expect(card).not_to_contain_text("Private second line")
        expect(card).to_contain_text("2 fichiers")
        expect(card).to_contain_text("1 conversation")

    def test_project_counts_turn_labels_and_disclosures_are_discreet(self) -> None:
        from playwright.sync_api import expect
        from project_composer_browser import (
            CONVERSATION,
            ORG,
            PROJECT,
            SESSION,
            VERSION,
        )

        f = self.fixture
        project = str(f.project_items[0]["id"])
        f.context.route(
            "**/projects/" + project + "/instructions",
            lambda route: route.fulfill(json={"text": "Budget"}),
        )
        f.context.route(
            "**/projects/" + project + "/files",
            lambda route: route.fulfill(json={"items": [{}], "next_cursor": None}),
        )
        f.context.route(
            "**/projects/" + project + "/conversations",
            lambda route: route.fulfill(json={"items": [], "next_cursor": None}),
        )
        f.open_projects()
        card = f.page.locator("[data-project-card]").first
        expect(card.locator(".atlas-project-card-counts")).to_have_text(
            "1 fichier · 0 conversation"
        )

        f.turns = [
            dict(
                id=VERSION,
                request_id=SESSION,
                state="completed",
                content_refs=[
                    dict(kind="question", text="Ma question"),
                    dict(kind="answer", text="La réponse d’Atlas"),
                ],
                usage={"context": {"layers": {"system": 1}}},
            )
        ]
        f.events = [
            dict(
                org_id=ORG,
                project_id=PROJECT,
                version=1,
                request_id=SESSION,
                conversation_id=CONVERSATION,
                seq=1,
                type="tool",
                status="completed",
            )
        ]
        f.context.unroute("**/projects/" + project + "/conversations")
        f.page.goto("http://127.0.0.1:18790/")
        f.open_projects()
        f.page.locator(f'[data-project-card="{project}"]').click()
        f.page.locator("[data-conversation]").click()
        expect(f.page.locator(".atlas-project-turn .question")).to_have_text(
            "Ma question"
        )
        expect(f.page.locator(".atlas-project-turn .answer")).to_have_text(
            "La réponse d’Atlas"
        )
        expect(
            f.page.locator(".atlas-project-turn").filter(has_text="Ma question")
        ).not_to_contain_text("Atlas ·")
        expect(
            f.page.locator(".atlas-project-turn").filter(has_text="La réponse d’Atlas")
        ).to_contain_text("Atlas · Terminé")
        disclosures = f.page.locator(".atlas-project-disclosure")
        expect(disclosures).to_have_count(2)
        self.assertEqual(
            disclosures.nth(0).evaluate("n=>getComputedStyle(n).fontSize"),
            disclosures.nth(1).evaluate("n=>getComputedStyle(n).fontSize"),
        )
        self.assertEqual(
            disclosures.nth(0).evaluate("n=>getComputedStyle(n).fontWeight"),
            disclosures.nth(1).evaluate("n=>getComputedStyle(n).fontWeight"),
        )


if __name__ == "__main__":
    unittest.main()
