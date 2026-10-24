"""2026-10-24: mixed provider reference frames must not consume answer text."""

import json
import unittest

from streaming import StreamDecoder
from services.orchestrator.source_truth import (
    inline_source_markers,
    without_unretrieved,
)


def feed(decoder: StreamDecoder, content: object) -> list[dict[str, object]]:
    return decoder.feed(
        (
            "data: "
            + json.dumps({"choices": [{"index": 0, "delta": {"content": content}}]})
            + "\n\n"
        ).encode()
    )


class ReferenceTextTests(unittest.TestCase):
    def test_mixed_chunk_keeps_word_endings_and_order(self) -> None:
        decoder = StreamDecoder()
        events = feed(decoder, "Le retour est géné")
        events += feed(
            decoder,
            [
                {"type": "text", "text": "ralisé"},
                {"type": "reference", "reference_ids": ["0"]},
                {"type": "text", "text": ". La suite"},
            ],
        )
        self.assertEqual(
            decoder.text, "Le retour est généralisé[[atlas-reference:0]]. La suite"
        )
        pieces = []
        for event in events:
            delta = event["delta"]
            assert isinstance(delta, dict)
            pieces.append(str(delta["content"]))
        self.assertEqual("".join(pieces), decoder.text)

    def test_unresolved_numeric_reference_list_does_not_leave_separators(self) -> None:
        raw = "Answer[[atlas-reference:900]],[[atlas-reference:901]]."
        self.assertEqual(
            inline_source_markers(without_unretrieved(raw, set()), []), "Answer."
        )

    def test_known_reference_does_not_consume_following_prose(self) -> None:
        url = "https://example.org/report"
        raw = "Use [[atlas-reference:0]].NET, e.g. for this tool."
        self.assertEqual(
            inline_source_markers(without_unretrieved(raw, {url}), [url]),
            f"Use [1]({url}).NET, e.g. for this tool.",
        )

    def test_captured_reference_then_host_suffix(self) -> None:
        # Exact typed shape and text splits of the live Trappes frames 284-290.
        decoder = StreamDecoder()
        for content in [
            " religion",
            [
                {"type": "reference", "reference_ids": ["elysee"]},
                {"type": "text", "text": ".fr/em"},
            ],
            "manuel-macron",
            "/202",
            "6/10",
            "/23/de",
            "placement-a-trappes",
            ".\n",
        ]:
            feed(decoder, content)
        url = "https://www.elysee.fr/emmanuel-macron/2026/10/23/deplacement-a-trappes"
        rendered = inline_source_markers(
            without_unretrieved(decoder.text, {url}), [url]
        )
        self.assertEqual(rendered, f" religion [1]({url}).\n")

    def test_unresolved_citation_run_leaves_prose_and_punctuation(self) -> None:
        decoder = StreamDecoder()
        for content in [
            "Le retour est géné",
            [
                {"type": "text", "text": "ralisé"},
                {"type": "reference", "reference_ids": ["unknown"]},
                {"type": "text", "text": ".fr2026"},
            ],
            "1023,lemonde.fr20261023",
            ". La suite.",
        ]:
            feed(decoder, content)
        self.assertEqual(
            without_unretrieved(decoder.text, set()),
            "Le retour est généralisé. La suite.",
        )

    def test_captured_iso_dated_reference_list_keeps_complete_words(self) -> None:
        decoder = StreamDecoder()
        for content in [
            "La situation reste non résolue",
            [
                {"type": "reference", "reference_ids": ["selectra"]},
                {"type": "text", "text": ".info20"},
            ],
            "26-1",
            "0-24",
            ",justgeek.fr2026-10-24,blog-nouvelles-technologies.fr2026-10-24.",
        ]:
            feed(decoder, content)
        self.assertEqual(
            without_unretrieved(decoder.text, set()),
            "La situation reste non résolue.",
        )

    def test_captured_adjacent_reference_cannot_be_consumed_as_url_path(self) -> None:
        raw = (
            "La religion[[atlas-reference:ladepeche]].fr/2026/10/23/report,"
            "elysee.fr/2026/10/23/report.md[[atlas-reference:elysee]].fr/"
            "2026/10/23/deplacement-a-trappes."
        )
        self.assertEqual(without_unretrieved(raw, set()), "La religion.")

    def test_captured_citation_list_consumes_its_separated_date(self) -> None:
        raw = (
            "Le retour reste progressif[[atlas-reference:justgeek]].fr,"
            "selectra.info,blog-nouvelles-technologies.fr,2026-10-21."
        )
        self.assertEqual(without_unretrieved(raw, set()), "Le retour reste progressif.")

    def test_known_reference_keeps_following_date_in_prose(self) -> None:
        url = "https://example.org/report"
        raw = "Report [[atlas-reference:0]],2026-10-21."
        self.assertEqual(
            inline_source_markers(without_unretrieved(raw, {url}), [url]),
            f"Report [1]({url}),2026-10-21.",
        )


class ReferenceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_adjacent_references_and_separated_date_at_every_boundary(
        self,
    ) -> None:
        from services.orchestrator.chat_schema import ChatRequest

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Cite the reports."}]}
        )
        raw = (
            "Le retour reste progressif[[atlas-reference:justgeek]].fr,"
            "selectra.info,2026-10-21.\n"
            "La religion[[atlas-reference:ladepeche]].fr/2026/report,"
            "elysee.fr/2026/report.md[[atlas-reference:elysee]].fr/2026/report."
        )
        for sources in ([], ["https://example.org/report"]):
            await self.check_delivery(
                request,
                sources,
                raw,
                "Le retour reste progressif.\nLa religion.",
            )

    async def test_iso_dated_reference_list_at_every_boundary(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Cite the outage report."}]}
        )
        raw = (
            "Le réseau reste perturbé[[atlas-reference:selectra]].info2026-10-24,"
            "justgeek.fr2026-10-24,blog-nouvelles-technologies.fr2026-10-24.\n"
            "La suite reste entière."
        )
        for sources in ([], ["https://selectra.info/telecom/report"]):
            await self.check_delivery(
                request,
                sources,
                raw,
                "Le réseau reste perturbé.\nLa suite reste entière.",
            )

    async def test_captured_shape_in_json_and_sse_at_every_boundary(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest

        url = "https://www.elysee.fr/emmanuel-macron/2026/10/23/deplacement-a-trappes"
        decoder = StreamDecoder()
        for content in [
            "Le retour est géné",
            [
                {"type": "text", "text": "ralisé"},
                {"type": "reference", "reference_ids": ["elysee"]},
                {"type": "text", "text": ".fr/em"},
            ],
            "manuel-macron/2026/10/23/deplacement-a-trappes",
            ".\nLa suite reste entière.",
        ]:
            feed(decoder, content)
        raw = decoder.text
        expected = f"Le retour est généralisé [1]({url}).\nLa suite reste entière."
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Cite the report."}]}
        )
        for sources, source_text, answer_text in (
            ([url], raw, expected),
            ([], raw, "Le retour est généralisé.\nLa suite reste entière."),
        ):
            await self.check_delivery(request, sources, source_text, answer_text)

    async def check_delivery(
        self, request: object, sources: list[str], raw: str, expected: str
    ) -> None:
        import os
        import tempfile
        import time
        from unittest.mock import patch

        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.chat_stream import response
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        assert isinstance(request, ChatRequest)
        for split in range(len(raw) + 1):
            with self.subTest(split=split):

                async def generate(request: ChatRequest, item: Interaction) -> None:
                    item.retrieved_urls = sources
                    item.task_type = "text"
                    item.state = "done"
                    item.reponse = raw
                    if sink := sink_context.get():
                        for part in (raw[:split], raw[split:]):
                            await sink({"delta": {"content": part}})

                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
                    patch("services.orchestrator.chat_pipeline._process", generate),
                ):
                    item = Interaction()
                    await process(request, item)
                    chunks = [
                        str(chunk)
                        async for chunk in response(
                            request, Interaction(), time.monotonic(), process
                        ).body_iterator
                    ]
                events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
                answer = "".join(
                    e.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    for e in events
                )
                self.assertEqual(item.reponse.split("\n\n<details>")[0], expected)
                self.assertEqual(answer.split("\n\n<details>")[0], expected)
