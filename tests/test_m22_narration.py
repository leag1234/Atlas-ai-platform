"""M22 narration must never reach streamed or stored answer content."""

import unittest
from services.orchestrator.narration import (
    NarrationFilter,
    ProtocolFilter,
    clean_answer,
    protocol_material,
    unfinished_announcement,
)


class NarrationTests(unittest.TestCase):
    def test_chunk_boundaries_and_activity(self) -> None:
        source = "Je vais rechercher des informations. Let me verify the date. Le prix est 135 USD."
        for size in range(1, len(source)):
            parser = NarrationFilter()
            output = "".join(
                parser.feed(source[i : i + size]) for i in range(0, len(source), size)
            )
            output += parser.finish()
            self.assertEqual(output.strip(), "Le prix est 135 USD.")
            self.assertEqual(len(parser.removed), 2)

    def test_substantive_quoted_and_code_text_preserved(self) -> None:
        for text in (
            "Je vais à Paris demain.",
            "Let me explain the calculation: 2 + 2 = 4.",
            "> Je vais rechercher des informations.",
            "```text\nLet me verify the date.\n```",
            "Il a dit « Je vais rechercher ».",
            "Le prix est 135.50 USD.",
        ):
            self.assertEqual(clean_answer(text), text)

    def test_empty_narration_is_not_success(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty_content_after_narration"):
            clean_answer(
                "Je vais consulter une source. Je peux maintenant synthétiser."
            )

    def test_working_notes_are_removed_but_explanatory_let_me_survives(self) -> None:
        text = (
            "Let me try one more targeted search. The search results don't identify "
            "the entity. The available record is incomplete."
        )
        self.assertEqual(clean_answer(text), "The available record is incomplete.")
        self.assertEqual(
            clean_answer("Let me explain the calculation: 2 + 2 = 4."),
            "Let me explain the calculation: 2 + 2 = 4.",
        )

    def test_protocol_filter_holds_internal_json_but_keeps_code_quote(self) -> None:
        leaked = 'Answer. {"scope":"CURRENT REQUEST: hidden"}'
        parser = ProtocolFilter()
        output = (
            "".join(parser.feed(leaked[i : i + 1]) for i in range(len(leaked)))
            + parser.finish()
        )
        self.assertEqual(output, "Answer. ")
        self.assertEqual(len(parser.removed), 1)
        code = '```json\n{"scope":"CURRENT REQUEST: quoted example"}\n```'
        parser = ProtocolFilter()
        self.assertEqual(parser.feed(code) + parser.finish(), code)
        self.assertFalse(protocol_material(code))

    def test_protocol_filter_holds_raw_tool_call(self) -> None:
        raw = '{"name":"web_search","arguments":{"query":"example"}}'
        parser = ProtocolFilter()
        self.assertEqual(parser.feed(raw) + parser.finish(), "")
        self.assertEqual(len(parser.removed), 1)

    def test_search_announcement_is_identified_only_when_it_has_no_answer(self) -> None:
        self.assertTrue(
            unfinished_announcement("I need to search for current sources.")
        )
        self.assertFalse(
            unfinished_announcement("The search index field is named `status`.")
        )
