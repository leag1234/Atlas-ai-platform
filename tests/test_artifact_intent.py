"""Tests for interactive app request classification."""

import unittest

from packages.artifact_intent import app_intent


HISTORY = (
    {
        "role": "assistant",
        "content": (
            "[Open Snake](/api/atlas/artifacts/"
            "0123456789abcdef0123456789abcdef/versions/1/open)"
        ),
    },
)


class ArtifactIntentTests(unittest.TestCase):
    def test_create_in_five_languages(self) -> None:
        requests = (
            "Fais un jeu de serpent",
            "Make a timer app",
            "Erstelle ein kleines Spiel",
            "Haz una calculadora",
            "Fai un quiz",
        )
        for text in requests:
            with self.subTest(text=text):
                self.assertEqual(app_intent(text, ()), "create")

    def test_explicit_web_stack_and_named_games(self) -> None:
        for text in (
            "Write an HTML page",
            "Crée un Pong en JavaScript",
            "Build a canvas visualization",
            "Génère un tableau de bord",
        ):
            with self.subTest(text=text):
                self.assertEqual(app_intent(text, ()), "create")

    def test_required_negatives(self) -> None:
        requests = (
            "Écris un jeu en Python",
            "Programme un jeu avec pygame",
            "Comment faire un jeu HTML ?",
            "Explain how to make a web app",
            "Crée un jeu en PDF",
            "Dessine un jeu",
            "Écris un titre pour une page web",
            "Suggest a title for a landing page",
        )
        for text in requests:
            with self.subTest(text=text):
                self.assertIsNone(app_intent(text, ()))

    def test_other_excluded_requests(self) -> None:
        requests = (
            "Create a game with an image",
            "[Attachment: game.txt] Make a game",
            "Create a game in project mode",
            "Create a game and use my API key",
            "Create a game in Flask",
            "Create a game as a DOCX",
        )
        for text in requests:
            with self.subTest(text=text):
                self.assertIsNone(app_intent(text, ()))

    def test_office_formats_are_not_app_requests_in_five_languages(self) -> None:
        requests = (
            "Fais une présentation PowerPoint sur le climat",
            "Make a PowerPoint presentation about climate",
            "Erstelle eine PowerPoint-Präsentation über das Klima",
            "Haz una presentación de PowerPoint sobre el clima",
            "Fai una presentazione PowerPoint sul clima",
        )
        for text in requests:
            with self.subTest(text=text):
                self.assertIsNone(app_intent(text, ()))

    def test_explicit_web_request_wins_over_office_format(self) -> None:
        self.assertEqual(
            app_intent("Create a presentation as an HTML page", ()),
            "create",
        )

    def test_history_states(self) -> None:
        self.assertEqual(app_intent("Ajoute un bouton", HISTORY), "modify")
        self.assertEqual(app_intent("Make it green", HISTORY), "modify")
        self.assertEqual(app_intent("Change la couleur ?", HISTORY), None)
        self.assertEqual(app_intent("Fais un autre jeu", HISTORY), "available")
        self.assertEqual(app_intent("Ajoute un bouton", ()), "create")

    def test_history_ignores_non_artifact_links(self) -> None:
        history = ({"role": "assistant", "content": "See example.com"},)
        self.assertEqual(app_intent("Ajoute un bouton", history), "create")


if __name__ == "__main__":
    unittest.main()


class SvgIntentTests(unittest.TestCase):
    def test_svg_drawing_and_show_it_here_are_app_turns(self) -> None:
        from packages.artifact_intent import app_intent

        svg_answer = [
            {"role": "user", "content": "dessine un mouton en svg"},
            {
                "role": "assistant",
                "content": "```svg\n<svg viewBox='0 0 10 10'></svg>\n```",
            },
            {"role": "user", "content": "tu peux l'afficher ici ?"},
        ]
        self.assertEqual(app_intent("dessine-moi un mouton en SVG", []), "create")
        self.assertEqual(app_intent("draw a sheep as an SVG", []), "create")
        self.assertEqual(app_intent("tu peux l'afficher ici ?", svg_answer), "create")

    def test_plain_drawing_explanation_and_unrelated_show_are_not(self) -> None:
        from packages.artifact_intent import app_intent

        self.assertIsNone(app_intent("dessine un chat", []))
        self.assertIsNone(app_intent("explique le format SVG", []))
        self.assertIsNone(app_intent("montre-moi la météo", []))
        self.assertIsNone(
            app_intent("affiche-le ici", [{"role": "assistant", "content": "Bonjour"}])
        )
