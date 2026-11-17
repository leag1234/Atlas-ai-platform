"""Routing words are read in the person's request, never in a pasted document.

Pilot 2026-11-17: a summary check sent with its full source text started a web
search because of words inside the source ("monnaie", "current", "links"), and
"the facts it links" or "how the tunnel links two countries" was read as a request
for web links, so the answer was replaced by "I could not retrieve verified links".
"""

import unittest

from services.orchestrator.link_requests import link_request, own_request, page_request
from services.orchestrator.search_policy import required_research, requested_page_read

# Fictional article: every routing word of the detectors appears in it.
ARTICLE = "\n\n".join(
    [
        "Pont de Valcrest : quatre ans de chantier",
        "La première pierre du pont de Valcrest est posée le 3 mars 1998 et l'ouvrage "
        "ouvre le 12 juin 2002. Il relie les deux rives de la Sorne, et ce lien entre les "
        "deux bourgs change la vie locale. Le prix actuel du péage est affiché sur les "
        "links du site officiel, et les habitants peuvent buy un abonnement mensuel. "
        "L'ingénieure Lena Marsh dirige le chantier depuis la première version des plans.",
        "Chaque pile repose sur une semelle de quatre mètres. Les caissons sont soudés "
        "à l'arrière de la culée puis poussés au-dessus du vide, où se trouve un appui "
        "provisoire. Une pièce de monnaie commémorative est frappée pour l'ouverture, "
        "et la dernière version du logiciel de suivi mesure les déplacements en continu, "
        "today comme au premier jour. La latest inspection confirme l'état des câbles.",
        "Le tablier pèse trente-six mille tonnes. Il est lancé par étapes de cent "
        "soixante mètres, avec des vérins pilotés depuis un poste central. Les "
        "équipes travaillent en trois postes et le chantier ne compte aucun accident "
        "grave pendant les quatre années de construction du pont et de ses accès.",
    ]
)
SUMMARY = (
    "Résumé : le pont de Valcrest ouvre en 2002 après quatre ans de travaux ; "
    "son tablier de 36 000 tonnes est lancé par étapes."
)


def message(instruction: str) -> str:
    return instruction + "\n\nTEXTE :\n" + ARTICLE + "\n\n" + SUMMARY


class PastedDocumentRoutingTests(unittest.TestCase):
    def test_short_messages_are_their_own_request(self) -> None:
        for text in ("Explain a triangle", "x" * 1600, ARTICLE[:600]):
            self.assertEqual(own_request(text), text)

    def test_document_is_removed_around_the_persons_lines(self) -> None:
        text = "Peux-tu résumer ce texte ?\n\n" + ARTICLE + "\n\nMerci, en 3 phrases."
        self.assertEqual(
            own_request(text),
            "Peux-tu résumer ce texte ?\n\nPont de Valcrest : quatre ans de chantier"
            "\n\nMerci, en 3 phrases.",
        )

    def test_checking_or_summarising_a_pasted_text_starts_no_research(self) -> None:
        for instruction, language in (
            ("Vérifie si ce résumé est fidèle au texte stp.", "fr"),
            ("pls check this summary against the text, any errors?", "en"),
            ("Fass bitte diesen Text in drei Sätzen zusammen:", "de"),
            ("Compara este resumen con el texto y dime si hay errores.", "es"),
            ("この要約が原文に忠実か確認してください。", "ja"),
        ):
            with self.subTest(language=language):
                text = message(instruction)
                self.assertEqual(required_research(text, language), ())
                self.assertFalse(page_request(text))
                self.assertFalse(requested_page_read(text))

    def test_the_persons_own_request_still_routes(self) -> None:
        current = required_research(
            message("Cherche sur le web le prix actuel du péage."), "fr"
        )
        self.assertEqual([call.name for call in current], ["web_search"])
        self.assertTrue(
            page_request(message("Donne-moi des liens vers les pages officielles."))
        )

    def test_relation_and_verb_senses_of_link_are_not_link_requests(self) -> None:
        for question in (
            "whats the link between sleep and memory?",
            "Quel est le lien entre le tabac et le cancer du poumon ?",
            "c quoi le lien direct entre inflation et taux ?",
            "Gibt es einen Link zwischen Schlaf und Gedächtnis?",
            "Explain briefly how the tunnel links two countries.",
            "Explain how the Valcrest Bridge links Northbank and Southbank.",
            "The passages must state that relation itself, not only the facts it links.",
            "Où se trouve le pont de Valcrest exactement ?",
            "ou se trouvent les piles du pont ?",
        ):
            with self.subTest(question=question):
                self.assertFalse(page_request(question))
                self.assertEqual(
                    [
                        c.id
                        for c in required_research(question, "en")
                        if c.id.startswith("required-links")
                    ],
                    [],
                )

    def test_requests_for_links_still_route(self) -> None:
        for question in (
            "send me the link please",
            "Can you add links to the sources?",
            "Links for those",
            "des liens pour ces modèles",
            "Où trouver des chaussures de rando pas chères ?",
            "Where can I get a cheap rail pass?",
            "Any URL for the official page?",
        ):
            with self.subTest(question=question):
                self.assertTrue(link_request(question))

    def test_pilot_shaped_check_prompt(self) -> None:
        prompt = (
            "You verify claims against PASSAGES from a source document. For each claim "
            "give a verdict:\n- supported: the passages state it (paraphrase or "
            "translation is fine). For a cause, order, negation, quantity or attribution "
            "claim, the passages must state that relation itself, not only the facts it "
            "links.\n- contradicted: the passages state something incompatible.\n"
            "- not_found: the passages do not state it.\nOutput only JSON.\n"
            "\nPASSAGES:\n" + ARTICLE.replace("\n\n", "\n") + "\n\nCLAIMS:\n1. (fact) "
            "The bridge opened in 2002."
        )
        self.assertFalse(page_request(prompt))
        self.assertEqual(required_research(prompt, "en"), ())


if __name__ == "__main__":
    unittest.main()
