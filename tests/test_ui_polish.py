"""Offline regressions for Open WebUI finishing touches."""

import json
import unittest

from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.source_truth import inline_source_markers

TITLE_REQUEST = """### Task:
Generate a concise title summarizing the chat history.
### Output:
JSON format: { "title": "your concise title here" }
### Chat History:
<chat_history>
USER: Comment préparer un potager sur balcon ?
ASSISTANT: Voici les étapes.
</chat_history>"""


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_openwebui_title_task_is_deterministic_and_french(self) -> None:
        request = ChatRequest.model_validate(
            {
                "ui_locale": "en",
                "messages": [{"role": "user", "content": TITLE_REQUEST}],
            }
        )
        item = Interaction()
        await process(request, item)
        self.assertEqual(item.state, "done")
        self.assertEqual(item.task_type, "title")
        self.assertEqual(item.cout_eur, 0.0)
        title = json.loads(item.reponse)["title"]
        self.assertLessEqual(len(title.split()), 6)
        self.assertIn("potager", title.lower())


class SourceMarkerTests(unittest.TestCase):
    def test_inline_source_marker_keeps_the_same_url(self) -> None:
        url = "https://example.org/article"
        self.assertEqual(
            inline_source_markers(f"Voir [source]({url}).", [url]), f"Voir [1]({url})."
        )

    def test_bare_retrieved_url_becomes_a_marker(self) -> None:
        url = "https://example.org/a"
        self.assertEqual(
            inline_source_markers(f"Selon {url}, oui.", [url]),
            f"Selon [1]({url}), oui.",
        )

    def test_uncited_answer_gets_no_invented_marker(self) -> None:
        text = "Réponse sans citation."
        self.assertEqual(inline_source_markers(text, ["https://example.org/a"]), text)

    def test_unretrieved_link_is_left_alone(self) -> None:
        text = "Voir [x](https://other.example/b)."
        self.assertEqual(inline_source_markers(text, ["https://example.org/a"]), text)
