"""The journey classifier rejects English and ignores code blocks."""

import unittest
from journey_language import matches


class LanguageTests(unittest.TestCase):
    def test_french_english_and_code(self) -> None:
        self.assertTrue(
            matches("Dans cette image, il y a un carré rouge et un cercle bleu.", "fr")
        )
        self.assertFalse(
            matches("The image shows a red square and a blue circle.", "fr")
        )
        self.assertTrue(
            matches("The image shows a red square and a blue circle.", "en")
        )
        self.assertTrue(
            matches(
                "La capitale de la France est Paris.\n```The image shows a red square.```",
                "fr",
            )
        )
        self.assertFalse(matches("```La capitale de la France est Paris.```", "fr"))

    def test_source_titles_and_activity_do_not_determine_answer_language(self) -> None:
        metadata = "<details>Recherche web en cours. Lecture de page.</details>"
        citation = "\nSource : [The birth of the World Wide Web](https://home.cern/science/computing/the-birth-of-the-web/), consulté le 2026-09-17."
        french = "Le CERN a rendu le World Wide Web public le 30 avril 1993 en plaçant son logiciel dans le domaine public."
        english = "The laboratory released the software into the public domain, allowing anyone to use and improve it."
        self.assertTrue(matches(metadata + french + citation, "fr"))
        self.assertFalse(matches(metadata * 10 + english + citation, "fr"))
        self.assertFalse(
            matches(metadata + "[An English source](https://example.org)", "fr")
        )


class UppercaseLanguageTests(unittest.TestCase):
    def test_uppercase_instruction_retains_french_prose_language(self) -> None:
        from packages.language import detected_language

        text = "CALCULE UNE MOYENNE EN PYTHON AVEC UNE DOCSTRING ET UN EXEMPLE."
        self.assertEqual(detected_language(text), "fr")
        self.assertEqual(detected_language(text), detected_language(text.lower()))
