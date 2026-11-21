"""Links to app versions Atlas never stored are removed from answers (artifact_links).

Staging 2026-11-20: a plain answer wrote its own footer for a "version 5" of
the conversation's app that was never stored; the side panel opened an empty
page and the next change rewrote the app from scratch.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from services.orchestrator import artifact_store
from services.orchestrator.artifact_links import Rewriter, checked
from services.orchestrator.artifact_tools import footer
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context

OWNER = "owner@example.test"
APP = "<!DOCTYPE html><html><body><button>Go</button></body></html>"


def streamed(rewriter: Rewriter, text: str, size: int) -> str:
    out = [rewriter.feed(text[i : i + size]) for i in range(0, len(text), size)]
    return "".join(out) + rewriter.finish()


class RewriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stored = {("a" * 32, 1), ("a" * 32, 2)}

    def rewriter(self) -> Rewriter:
        return Rewriter(
            "fr",
            lambda artifact_id, version: (artifact_id, version) in self.stored,
            lambda artifact_id: 2 if artifact_id == "a" * 32 else None,
        )

    def test_an_invented_version_is_replaced_by_the_unchanged_sentence(self) -> None:
        invented = footer("a" * 32, 5, "Boîte à rythme", "boite.html", "fr")
        text = "Voici la version 5 avec de vrais sons.\n\n" + invented + "\n\nFin."
        for size in (1, 2, 7, 50, len(text)):
            with self.subTest(size=size):
                result = streamed(self.rewriter(), text, size)
                self.assertNotIn("/api/atlas/artifacts/", result)
                self.assertIn(
                    "La modification n'a pas pu être appliquée ; la version 2 "
                    "reste inchangée.",
                    result,
                )
                self.assertNotIn(" · ", result)
                self.assertTrue(result.endswith("\n\nFin."))

    def test_a_stored_version_and_other_links_are_kept_as_written(self) -> None:
        text = (
            "See [the docs](https://example.org/a) and [x] [y](z).\n\n"
            + footer("a" * 32, 2, "App [beta]", "app.html", "en")
            + "\n\n[label](/api/atlas/other)"
        )
        for size in (1, 3, len(text)):
            with self.subTest(size=size):
                self.assertEqual(streamed(self.rewriter(), text, size), text)

    def test_an_unknown_artifact_is_removed_without_a_sentence(self) -> None:
        text = "Ici : " + footer("b" * 32, 2, "App", "app.html", "fr") + " ok"
        rewriter = self.rewriter()
        self.assertEqual(streamed(rewriter, text, 4), "Ici :  ok")
        self.assertEqual(rewriter.removed, 2)

    def test_an_open_bracket_is_released_when_no_link_can_follow(self) -> None:
        rewriter = self.rewriter()
        self.assertEqual(rewriter.feed("a [b"), "a ")
        self.assertEqual(rewriter.feed("] c"), "[b] c")
        self.assertEqual(rewriter.feed(" [d]("), " ")
        self.assertEqual(rewriter.feed("/api/x) e"), "[d](/api/x) e")
        self.assertEqual(rewriter.finish(), "")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"ATLAS_DATA_DIR": self.directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_versions_are_looked_up_for_the_owner(self) -> None:
        created = artifact_store.create(OWNER, "App", "app.html", APP.encode(), "r1")
        artifact_id = str(created["id"])
        real = footer(artifact_id, 1, "App", "app.html", "en")
        invented = footer(artifact_id, 2, "App", "app.html", "en")
        self.assertEqual(checked(real, "en", OWNER), (real, 0))
        text, removed = checked("Done.\n\n" + invented, "en", OWNER)
        self.assertEqual(
            text, "Done.\n\nThe change could not be applied; version 1 is unchanged."
        )
        self.assertEqual(removed, 2)
        # Another person's artifact is not theirs to link to.
        self.assertEqual(checked(real, "en", "other@example.test")[1], 2)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(
            os.environ,
            {
                "ATLAS_DATA_DIR": self.directory.name,
                "ATLAS_ARTIFACTS": "1",
                "ATLAS_SOURCE_FAITHFULNESS": "off",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    async def test_a_plain_answer_cannot_announce_a_version_it_did_not_store(
        self,
    ) -> None:
        created = artifact_store.create(OWNER, "Drums", "d.html", APP.encode(), "r1")
        artifact_id = str(created["id"])
        answer = "Here is version 2 with real samples.\n\n" + footer(
            artifact_id, 2, "Drums", "d.html", "en"
        )

        async def generate(request: ChatRequest, item: Interaction) -> None:
            upstream = sink_context.get()
            assert upstream is not None
            for start in range(0, len(answer), 9):
                await upstream({"delta": {"content": answer[start : start + 9]}})
            item.reponse = answer
            item.state = "done"

        sent: list[str] = []

        async def sink(event: dict[str, object]) -> None:
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                sent.append(str(delta["content"]))

        request = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": footer(artifact_id, 1, "Drums", "d.html", "en"),
                    },
                    {"role": "user", "content": "here are the sample links"},
                ],
                "lang": "en",
                "atlas_ui": True,
                "stream": True,
            }
        )
        request.__dict__["artifact_owner"] = OWNER
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with patch(
                "services.orchestrator.chat_pipeline._process_request",
                side_effect=generate,
            ):
                await process(request, item)
        finally:
            sink_context.reset(token)
        expected = (
            "Here is version 2 with real samples.\n\n"
            "The change could not be applied; version 1 is unchanged."
        )
        self.assertEqual("".join(sent), expected)
        self.assertEqual(item.reponse, expected)


if __name__ == "__main__":
    unittest.main()
