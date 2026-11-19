"""Defects seen live on staging on 2026-11-17 (judge budget, list numbers)."""

import unittest

from services.orchestrator import source_faithfulness


class LiveDefectTests(unittest.TestCase):
    def test_the_judge_asks_no_more_than_the_assistant_output_limit(self) -> None:
        self.assertLessEqual(source_faithfulness.JUDGE_TOKENS, 6_000)

    def test_list_numbers_are_not_facts_to_find(self) -> None:
        text = "The cathedral was begun in 1248 and completed in 1880."
        source = source_faithfulness.Source(text=text, passages=[text], own=[])
        answer = (
            "Voici les points :\n1. Début en 1248.\n2) Arrêt du chantier.\n"
            "**3.** Achèvement en 1880.\n4. Fin."
        )
        self.assertEqual(source_faithfulness.hints(answer, source), [])

    def test_a_missing_number_is_still_reported(self) -> None:
        text = "The cathedral was begun in 1248 and completed in 1880."
        source = source_faithfulness.Source(text=text, passages=[text], own=[])
        found = source_faithfulness.hints("1. Achèvement en 1890.", source)
        self.assertEqual(found, ["1890"])


# Open WebUI's title task quotes the conversation inside a prompt longer than a
# pasted text (document_quotes.PASTED_MINIMUM): staging 2026-11-16 to 11-19, the
# quotation check stripped the quotes of the title's JSON value and the
# faithfulness check then judged the broken title (83 of 89 checks).
INTERFACE_TITLE_TASK = (
    "### Task:\nGenerate a concise title summarizing the chat history.\n"
    "### Guidelines:\n"
    "- The title should clearly represent the main theme or subject of the "
    "conversation.\n- Keep it short: 2-4 words is best.\n"
    "- Do not use emojis, quotation marks, or special formatting.\n"
    "- Write the title in the chat's primary language; default to English if "
    "multilingual.\n- Prioritize accuracy over excessive creativity.\n"
    '### Output:\nJSON format: { "title": "your concise title here" }\n'
    "### Chat History:\n<chat_history>\n"
    "USER: I need to be at the central station of Brennholm tomorrow by 2pm, "
    "how do I get there by train from the harbour district?\n"
    "ASSISTANT: Take the regional line 4 from Harbour Road towards the centre; "
    "it runs every twenty minutes and the trip takes about a quarter of an "
    "hour, so a departure around 1:20 pm leaves you a margin.\n"
    "</chat_history>"
)


class InterfaceTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_title_json_is_neither_quote_checked_nor_judged(self) -> None:
        from decimal import Decimal
        import json
        from unittest import mock

        from services.orchestrator import document_quotes
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction

        self.assertGreaterEqual(
            len(INTERFACE_TITLE_TASK), document_quotes.PASTED_MINIMUM
        )
        title = json.dumps({"title": "Train to Brennholm Central"})
        request = ChatRequest.model_validate(
            {
                "ui_locale": "en",
                "messages": [{"role": "user", "content": INTERFACE_TITLE_TASK}],
            }
        )
        item = Interaction()
        review = mock.AsyncMock(side_effect=AssertionError("judged"))
        with (
            mock.patch.dict(
                "os.environ",
                {"ATLAS_SOURCE_FAITHFULNESS": "on", "ATLAS_DOCUMENT_QUOTES": "mark"},
            ),
            mock.patch(
                "services.orchestrator.title.title_answer",
                mock.AsyncMock(return_value=(title, Decimal(0))),
            ),
            mock.patch.object(source_faithfulness, "review", review),
        ):
            await process(request, item)
        self.assertEqual(item.task_type, "title")
        self.assertEqual(item.reponse, title)
        self.assertEqual(
            json.loads(item.reponse), {"title": "Train to Brennholm Central"}
        )
        review.assert_not_called()
        self.assertFalse([s for s in item.steps if s.get("phase") == "source_check"])


if __name__ == "__main__":
    unittest.main()
