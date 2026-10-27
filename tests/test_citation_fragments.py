"""Owner regressions, 2026-10-24: tool citation debris in JSON and SSE."""

import gzip
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from streaming import StreamDecoder
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.source_truth import (
    inline_source_markers,
    source_footer,
    without_unretrieved,
)
from services.orchestrator.stream_client import sink_context

SOURCES = [
    "https://www.blog-nouvelles-technologies.fr/389385/"
    "panne-orange-sosh-21-octobre-2026-160334/",
    "https://selectra.info/telecom/.../",
    "https://www.jechange.fr/telecom/orange/panne",
    "https://www.statut-services.fr/service/orange",
]
OBSERVED = [
    (
        "...à cette heure-làpanne-orange-sosh-21-octobre-2026-160334/"
        "consulted_at=2026-10-24T08:13:27.244138+00:00,"
        "blog-nouvelles-technologies.fr/389385/panne-.../"
        "consulted_at=2026-10-24T08:13:27.413939+00:00,"
        "selectra.info/telecom/.../consulted_at=2026-10-23T11:58:16.095409+00:00.",
        "...à cette heure-là",
    ),
    (
        "**»**jechange.fr/telecom/orange/panne, "
        "consulted_at=2026-10-24T08:12:44.474887+00:00.",
        "**»**",
    ),
    ("au 23 octobre 2026-services.fr/service/orange", "au 23 octobre 2026"),
]


def render(text: str, sources: list[str] = SOURCES) -> str:
    return inline_source_markers(without_unretrieved(text, set(sources)), sources)


class CitationFragmentTests(unittest.TestCase):
    def test_exact_owner_fragments_preserve_prose_and_only_link_retrieval(self) -> None:
        for text, prose in OBSERVED:
            with self.subTest(text=text):
                answer = render(text)
                self.assertTrue(answer.startswith(prose))
                self.assertNotIn("consulted_at", answer)
                self.assertNotIn("+00:00", answer)
                for number, url in enumerate(SOURCES, 1):
                    answer = answer.replace(f"[{number}]({url})", "")
                self.assertNotIn("/", answer)
                self.assertNotIn(".fr", answer)
                self.assertNotIn(".info", answer)
                self.assertNotIn("panne-orange", answer)

    def test_retrieved_aliases_are_exact_and_numbering_is_stable(self) -> None:
        self.assertEqual(render(OBSERVED[1][0]), f"**»** [3]({SOURCES[2]}).")
        self.assertEqual(
            render(OBSERVED[2][0]), f"au 23 octobre 2026 [4]({SOURCES[3]})"
        )
        self.assertEqual(
            render(f"Known ({SOURCES[2]}, consulted_at=2026-10-24T08:12:44Z)."),
            f"Known ([3]({SOURCES[2]})).",
        )

    def test_unknown_aliases_and_metadata_cannot_become_sources(self) -> None:
        for text in (
            "Claim unknown.example/path, consulted_at=2026-10-24T08:12:44Z.",
            "Claim [[atlas-reference:https://unknown.example/path, consulted_at=2026-10-24]].",
            "Claim jechange.fr/telecom/orange/panne.",
        ):
            with self.subTest(text=text):
                answer = render(text, [])
                self.assertNotIn("/path", answer)
                self.assertNotIn("/telecom", answer)
                self.assertNotIn("consulted_at", answer)
                self.assertNotIn("atlas-reference", answer)

    def test_code_dates_images_and_existing_markdown_are_preserved(self) -> None:
        code = '```python\nconsulted_at="2026-10-24"\nurl="jechange.fr/telecom/orange/panne"\n```'
        text = "Ordinary date: 2026-10-24. `consulted_at=2026-10-24`\n" + code
        self.assertEqual(render(text), text)
        image = f"![Map]({SOURCES[2]})"
        self.assertEqual(render(image), image)
        linked = f"Claim [3]({SOURCES[2]})."
        self.assertEqual(render(linked), linked)
        self.assertEqual(render(render(OBSERVED[1][0])), render(OBSERVED[1][0]))

    def test_ambiguous_cut_path_is_removed_without_guessing_source(self) -> None:
        sources = [
            "https://one.example/long-article/",
            "https://two.example/long-article/",
        ]
        answer = render("Claim long-article/consulted_at=2026-10-24.", sources)
        self.assertEqual(answer, "Claim .")

    def test_reference_chunk_can_cut_hostname_before_later_plain_text(self) -> None:
        frames = [
            {"content": "au 23 octobre 2026"},
            {
                "content": [
                    {"type": "text", "text": "www.statut"},
                    {"type": "reference", "reference_ids": ["unknown"]},
                ]
            },
            {"content": "-services.fr/service/orange"},
        ]
        decoder = StreamDecoder()
        for delta in frames:
            decoder.feed(
                (
                    "data: "
                    + json.dumps({"choices": [{"index": 0, "delta": delta}]})
                    + "\n\n"
                ).encode()
            )
        self.assertEqual(render(decoder.text), f"au 23 octobre 2026 [4]({SOURCES[3]})")


class CitationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_rendered_answer_keeps_server_source_details(self) -> None:
        expected = render(OBSERVED[1][0]) + source_footer(SOURCES, "en")

        async def done(request: ChatRequest, item: Interaction) -> None:
            item.state, item.task_type = "done", "text"
            item.retrieved_urls = SOURCES.copy()
            item.reponse = expected

        request = ChatRequest.model_validate(
            {
                "atlas_ui": True,
                "messages": [{"role": "user", "content": "Is Orange down today?"}],
            }
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            chunks = [
                str(c)
                async for c in response(
                    request, Interaction(), time.monotonic(), done
                ).body_iterator
            ]
        events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
        answer = "".join(
            e.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for e in events
        )
        self.assertEqual(answer, expected)

    async def test_owner_fragments_through_json_and_stream_at_every_boundary(
        self,
    ) -> None:
        for raw, prose in OBSERVED:
            for split in range(len(raw) + 1):
                with self.subTest(raw=raw, split=split):

                    async def generate(request: ChatRequest, item: Interaction) -> None:
                        item.retrieved_urls = SOURCES.copy()
                        item.task_type = "text"
                        item.state = "done"
                        item.reponse = raw
                        if sink := sink_context.get():
                            for piece in (raw[:split], raw[split:]):
                                await sink({"delta": {"content": piece}})

                    request = ChatRequest.model_validate(
                        {
                            "atlas_ui": True,
                            "messages": [
                                {"role": "user", "content": "Is Orange down today?"}
                            ],
                        }
                    )
                    with (
                        tempfile.TemporaryDirectory() as directory,
                        patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
                        patch("services.orchestrator.chat_pipeline._process", generate),
                    ):
                        item = Interaction()
                        await process(request, item)
                        expected = item.reponse.split("\n\n<details>")[0]
                        chunks = [
                            str(c)
                            async for c in response(
                                request, Interaction(), time.monotonic(), process
                            ).body_iterator
                        ]
                    events = [
                        json.loads(c[6:]) for c in chunks if c.startswith("data: {")
                    ]
                    answer = "".join(
                        str(
                            e.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        for e in events
                    )
                    self.assertTrue(expected.startswith(prose))
                    self.assertNotIn("consulted_at", answer)
                    self.assertEqual(answer.split("\n\n<details>")[0], expected)
                    self.assertIn("<details>", item.reponse)

    def test_live_provider_recordings_remain_decodable(self) -> None:
        # These are actual provider bytes; synthetic boundary cases are separate above.
        path = Path("services/model-gateway/fixtures/citation-provider.sse.gz")
        decoder = StreamDecoder(allow_length=True)
        decoder.feed(gzip.decompress(path.read_bytes()))
        decoder.finish()
        self.assertIn("Orange", decoder.text)
        self.assertNotIn("consulted_at", render(decoder.text))
