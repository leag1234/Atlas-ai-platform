"""A pasted text is found in every layout, and never decides the answer's language.

Staging 2026-11-19 (stable-20261111-126): an English request to write an email
about a pasted German text got the email in German, because the language was
read in the whole message; and "TL;DR this" after a pasted email made of short
paragraphs started a web search (the email said "current budget") and the
summary got "No retrieved source mentions this entity.", because only a
paragraph longer than 200 bytes was taken for a pasted text. Fictional names
and texts only.
"""

import json
import time
import unittest
from types import SimpleNamespace

from packages.language import (
    conversation_language,
    own_prose,
    prose_language,
    requested_language,
)
from packages.images import VisionMessage
from packages.pasted_text import own_words as layout_own_words
from services.orchestrator.grounding import WORDING, Grounding
from services.orchestrator.link_requests import own_words, pasted_parts
from services.orchestrator.search_policy import required_research
from services.orchestrator.searxng import search_language

EMAIL = (
    "Hi team,\n\n"
    "The Brackenford library volunteers met on Tuesday. We agreed to move the "
    "book sale to the second weekend of June.\n\n"
    "Tomas Quillfeather will handle the posters. The bake stall needs two more "
    "helpers.\n\n"
    "The roof repair quote came in at 2,400 pounds, which is above the current "
    "budget.\n\n"
    "Thanks,\nInes"
)
GERMAN = (
    "Liebe Mitglieder des Gemeinschaftsgartens Moosbach,\n\n"
    "am Samstag findet unser Fr\u00fchjahrstreffen im Gartenhaus statt. Beginn ist um "
    "10 Uhr.\n\n"
    "Wir besprechen die neue Beetverteilung, die Reparatur des Wassertanks und den "
    "aktuellen Preis f\u00fcr das Sommerfest.\n\n"
    "Bitte bringt eigene Handschuhe mit. F\u00fcr Kaffee und Kuchen sorgt Frau "
    "Lindqvist-Hobel.\n\n"
    "Viele Gr\u00fc\u00dfe\nDer Vorstand"
)
GERMAN_LONG = (
    "Der Gemeinschaftsgarten Moosbach hat im vergangenen Jahr seine Fl\u00e4che "
    "verdoppelt. Nach langen Verhandlungen mit der Gemeinde wurde das brachliegende "
    "Grundst\u00fcck hinter der alten Molkerei in Pacht \u00fcbernommen, und seither "
    "bewirtschaften dort zweiundvierzig Familien eigene Beete.\n\n"
    "F\u00fcr das kommende Jahr plant der Verein eine Regenwasserzisterne mit "
    "achttausend Litern Fassungsverm\u00f6gen sowie einen kleinen Lehrpfad f\u00fcr "
    "Schulklassen, \u00fcber dessen Finanzierung im Fr\u00fchjahr entschieden wird."
)
FRENCH = (
    "La fanfare de Vaulmor\u00e9 r\u00e9p\u00e9tera d\u00e9sormais le jeudi soir dans la salle des "
    "f\u00eates. Le chef, Aur\u00e8le Pintemousse, demande \u00e0 chacun d'apporter son pupitre. "
    "Le prix actuel de la cotisation reste de 20 euros."
)
NOTES = (
    "- Riunione del comitato di Borgo Fiorlando: luned\u00ec alle 18\n"
    "- Ordine del giorno: nuova illuminazione della piazza\n"
    "- Preventivo ricevuto oggi: 3.200 euro\n"
    "- Responsabile: Ottavia Sbrancalupi"
)

# (request around the text, message, the request's own words)
LAYOUTS = (
    ("colon, blank line", "Summarize this email:\n\n" + EMAIL, "Summarize this email:"),
    ("after the text", EMAIL + "\n\nTL;DR this", "TL;DR this"),
    (
        "after, with a length",
        EMAIL + "\n\nSummarize this in three sentences.",
        "Summarize this in three sentences.",
    ),
    ("code fence", "Summarize:\n```\n" + EMAIL + "\n```", "Summarize:"),
    ("quotation", 'Summarize this text: "' + FRENCH + '"', "Summarize this text:"),
    (
        "curly quotation",
        "Summarize this:\n\u201c" + FRENCH + "\u201d",
        "Summarize this:",
    ),
    (
        "quote block",
        "Can you summarise this?\n\n"
        + "\n".join("> " + line for line in EMAIL.split("\n")),
        "Can you summarise this?",
    ),
    ("a short text", "R\u00e9sume ce texte :\n" + FRENCH, "R\u00e9sume ce texte :"),
    (
        "a list",
        "Write a summary of these notes\n" + NOTES,
        "Write a summary of these notes",
    ),
    (
        "a rule line",
        "Summarize the text below.\n---\n" + EMAIL + "\n---",
        "Summarize the text below.",
    ),
    ("same line", "TL;DR this: " + FRENCH, "TL;DR this:"),
    (
        "German request after",
        GERMAN + "\n\nKannst du das zusammenfassen?",
        "Kannst du das zusammenfassen?",
    ),
    (
        "mixed languages",
        "Fais-moi un r\u00e9sum\u00e9 de ce mail stp\n\n" + EMAIL,
        "Fais-moi un r\u00e9sum\u00e9 de ce mail stp",
    ),
)


def user(text: str) -> SimpleNamespace:
    return SimpleNamespace(role="user", text=text)


class PastedTextLayoutTests(unittest.TestCase):
    def test_every_layout_leaves_the_request_alone(self) -> None:
        for name, message, request in LAYOUTS:
            with self.subTest(layout=name):
                self.assertEqual(own_words(message).strip(), request)
                self.assertEqual(layout_own_words(message).strip(), request)
                self.assertTrue(pasted_parts(message))

    def test_no_layout_forces_a_search_on_the_pasted_text(self) -> None:
        for name, message, _ in LAYOUTS:
            with self.subTest(layout=name):
                self.assertEqual(required_research(message, "en"), ())

    def test_the_persons_own_lines_stay_whole(self) -> None:
        for message in (
            "What is the current price of the Brackenford library card?",
            "I live in Brennholm.\n\nWhat is the current price of a parking permit?",
            "We go to Valcrest next week with two kids, we like museums and "
            "parks.\n\nCan you check the current weather and list ideas?",
            "Answer these questions:\nWho runs Orlbrix today?\nWhat is its price?",
            "Can you check this?\nThe Orlbrix A7 costs 600 euros.",
            "Plan a trip for me.\nBudget 500 euros, from Valcrest, in March.",
            'What does "carpe diem" mean?',
            "Question: what is the latest version of the Orlbrix app, and when was "
            "it released, is it still supported?",
            "Write an email to my boss saying I will be late.\nAlso check the "
            "current train times from Valcrest to Brennholm tomorrow morning.",
            "Explain the difference between TCP and UDP.\nI read that UDP is "
            "faster but I do not see why.\nGive examples.",
            "Remind me at 10:30 to call the plumber about the leaking tap in the "
            "kitchen and the bathroom sink, it is urgent",
        ):
            with self.subTest(message=message[:40]):
                self.assertEqual(own_words(message), message)

    def test_a_real_question_beside_a_pasted_text_still_searches(self) -> None:
        message = (
            "What is the current price of a roof repair like the one in this "
            "email?\n\n" + EMAIL
        )
        calls = required_research(message, "en")
        self.assertEqual([call.name for call in calls], ["web_search"])
        query = json.loads(calls[0].arguments)["query"]
        self.assertNotIn("Brackenford", query)
        self.assertNotIn("Quillfeather", query)

    def test_a_summary_of_short_paragraphs_gets_no_missing_entity_note(self) -> None:
        question = EMAIL + "\n\nTL;DR this"
        summary = (
            "The Brackenford library volunteers moved the book sale to the second "
            "weekend of June; Tomas Quillfeather handles the posters and the roof "
            "repair quote of 2,400 pounds is above the budget."
        )
        checker = Grounding()
        checker.add("Practice test 4: the library of a small town sells old books.")
        checked = checker.check(summary, question, "en", messages=[question])
        self.assertEqual(checked, summary)
        self.assertNotIn(WORDING["missing"]["en"], checked)


class AnswerLanguageTests(unittest.TestCase):
    def test_the_request_decides_not_the_pasted_text(self) -> None:
        for request, text in (
            ("Write a short email to my neighbour about this German text:\n\n", GERMAN),
            (
                "Write a short email to my neighbour about this German text:\n\n",
                GERMAN_LONG,
            ),
            ("Write a short email to my neighbour about this text:\n", GERMAN_LONG),
            ("Summarize:\n```\n", GERMAN + "\n```"),
        ):
            for locale in ("en", "fr", "de"):
                with self.subTest(request=request[:30], text=text[:20], locale=locale):
                    messages = [user(request + text)]
                    language = conversation_language(messages, locale)
                    expected = "en" if "this" in request else locale
                    self.assertEqual(language, expected)
                    self.assertEqual(prose_language(messages, language), expected)

    def test_a_request_after_the_text_decides_too(self) -> None:
        messages = [
            user(
                GERMAN_LONG + "\n\nWrite a short email to my neighbour about this text."
            )
        ]
        self.assertEqual(conversation_language(messages, "fr"), "en")
        messages = [user(EMAIL + "\n\nKannst du das bitte kurz zusammenfassen?")]
        self.assertEqual(conversation_language(messages, "en"), "de")
        messages = [user("R\u00e9sume ce texte en trois phrases :\n\n" + GERMAN_LONG)]
        self.assertEqual(conversation_language(messages, "en"), "fr")

    def test_an_explicit_language_wins(self) -> None:
        message = (
            "Write a short email about this text, answer in German:\n\n" + GERMAN_LONG
        )
        self.assertEqual(requested_language(message), "de")
        self.assertEqual(
            requested_language("Translate this text into Italian:\n\n" + GERMAN), "it"
        )

    def test_a_pasted_texts_own_language_instruction_is_not_the_request(self) -> None:
        pasted = (
            "Please answer in French before Friday. The Brackenford library volunteers "
            "met on Tuesday and agreed to move the book sale to June.\n"
            "Tomas Quillfeather will handle the posters."
        )
        self.assertIsNone(requested_language("Summarize this email:\n\n" + pasted))

    def test_a_message_that_is_only_a_pasted_text_keeps_its_language(self) -> None:
        self.assertEqual(own_prose(GERMAN_LONG), GERMAN_LONG)
        self.assertEqual(conversation_language([user(GERMAN_LONG)], "en"), "de")

    def test_earlier_turns_are_read_in_their_own_words_too(self) -> None:
        messages = [
            user(
                "Write a short email to my neighbour about this German text:\n\n"
                + GERMAN
            ),
            SimpleNamespace(role="assistant", text="Here is a short email."),
            user("Shorter please"),
        ]
        self.assertEqual(conversation_language(messages, "fr"), "en")

    def test_the_search_language_follows_the_request(self) -> None:
        message = VisionMessage(
            role="user",
            content="What is the current price of a rain water tank like the one in "
            "this German text?\n\n" + GERMAN_LONG,
        )
        self.assertEqual(search_language([message], "fr"), "en")


class PastedTextCostTests(unittest.TestCase):
    def test_long_adversarial_messages_stay_linear(self) -> None:
        for text in (
            '"' * 200_000,
            ('"' + "a" * 120 + '" ') * 2_000,
            "> x\n" * 50_000,
            "```\n" * 50_000,
            "\n" * 200_000,
            ":" * 200_000,
            "this text " * 40_000,
            ('Summarize this:\n> a\n```\n"' + "b" * 50 + "\n") * 5_000,
        ):
            with self.subTest(text=text[:12]):
                start = time.perf_counter()
                layout_own_words(text)
                self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
