"""Owner regression 2026-10-22: diagrams and prose survive JSON and SSE delivery."""

from decimal import Decimal
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.stream_client import Sink

STIRLING = "Explain how a Stirling engine works with a diagram"
EXPLANATION = (
    "A Stirling engine heats and cools a sealed gas to move its pistons.\n\n"
    "Here is a schematic diagram illustrating the components and the flow of a "
    "typical alpha-type Stirling engine (which uses two power pistons):\n\n"
)
DIAGRAM = (
    '```mermaid\nflowchart LR\nH["Heat input"] --> E["Hot expansion piston"]\n'
    'E --> R["Regenerator"] --> C["Cold compression piston"]\n'
    'C --> R --> E\nE --> W["Crankshaft work"]\n```'
)


class DiagramDeliveryTests(unittest.TestCase):
    def request(
        self, stream: bool, question: str, replies: list[tuple[str, bool]]
    ) -> tuple[str, list[dict[str, object]]]:
        calls: list[dict[str, object]] = []
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=2048,
            ),
        )

        async def complete(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            calls.append(payload)
            text, image = replies[len(calls) - 1]
            return {
                "text": text,
                "calls": [
                    {
                        "id": "diagram-selection",
                        "name": "generate_image",
                        "arguments": json.dumps({"prompt": question}),
                    }
                ]
                if image
                else [],
                "usage": {"prompt_tokens": 120, "completion_tokens": 574},
                "observation": {"provider": "escalade", "route": "simple"},
                "cost_eur": "0",
            }

        async def receive(
            url: str, payload: dict[str, object], timeout: float, sink: Sink
        ) -> dict[str, object]:
            wire = await complete(url, payload, timeout)
            text = str(wire["text"])
            for start in range(0, len(text), 7):
                await sink({"delta": {"content": text[start : start + 7]}})
            return wire

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "0",
                    "ATLAS_TERMINAL_ENABLED": "0",
                    "ATLAS_INTERACTION_DIR": root,
                    "ATLAS_WEB_CACHE": root + "/cache",
                    "ATLAS_IMAGE_DIR": root + "/images",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(GatewayModel, "post", side_effect=complete),
            patch("services.orchestrator.model.receive", side_effect=receive),
            patch(
                "services.orchestrator.chat_pipeline.process_image", AsyncMock()
            ) as direct,
            patch(
                "services.orchestrator.image_tool.process_image", AsyncMock()
            ) as generated,
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "ui_locale": "en",
                    "stream": stream,
                    "messages": [{"role": "user", "content": question}],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            if stream:
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                self.assertFalse(any("error" in event for event in events), events)
                text = "".join(
                    event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    for event in events
                )
            else:
                text = response.json()["choices"][0]["message"]["content"]
            direct.assert_not_awaited()
            generated.assert_not_awaited()
        return text, calls

    def test_stirling_tool_selection_preserves_text_and_delivers_diagram(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                answer, calls = self.request(
                    stream, STIRLING, [(EXPLANATION, True), (DIAGRAM, False)]
                )
                self.assertIn(EXPLANATION.strip(), answer)
                self.assertIn(DIAGRAM, answer)
                self.assertEqual(len(calls), 2)

    def test_stirling_plain_answer_gets_one_bounded_diagram_completion(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                answer, calls = self.request(
                    stream, STIRLING, [(EXPLANATION, False), (DIAGRAM, False)]
                )
                self.assertIn(EXPLANATION.strip(), answer)
                self.assertIn(DIAGRAM, answer)
                self.assertEqual(len(calls), 2)

    def test_existing_mermaid_is_delivered_verbatim(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                answer, calls = self.request(
                    stream, STIRLING, [(EXPLANATION + DIAGRAM, False)]
                )
                self.assertEqual(answer, EXPLANATION + DIAGRAM)
                self.assertEqual(len(calls), 1)

    def test_diagram_failure_keeps_explanation_and_reports_missing_diagram(
        self,
    ) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                answer, calls = self.request(
                    stream,
                    STIRLING,
                    [(EXPLANATION, False), ("Heat drives the pistons.", False)],
                )
                self.assertIn(EXPLANATION.strip(), answer)
                self.assertIn("diagram could not be produced", answer)
                self.assertEqual(len(calls), 2)

    def test_markdown_survives_both_public_response_paths(self) -> None:
        for text in (
            "Before `![note](unknown)` after.",
            "```markdown\n![note](unknown)\n```\nAfter.",
            "~~~markdown\n![note](unknown)\n~~~\nAfter.",
            "Before ![note](unbalanced(the end.\nMore explanation.",
            "Wow![note] this is literal text.",
        ):
            for stream in (False, True):
                with self.subTest(text=text, stream=stream):
                    answer, _ = self.request(
                        stream, "Explain this notation", [(text, False)]
                    )
                    self.assertEqual(answer, text)

    def test_diagram_phrasings_and_picture_control(self) -> None:
        from services.orchestrator.diagram import requested
        from services.orchestrator.image_tool import explicit_image_request

        for question in (
            STIRLING,
            "Architecture flowchart",
            "SHOW THE COOLING CYCLE VISUALLY",
            "Map how requests travel between the browser and database",
            "Dessine le fonctionnement du moteur",
            "Sketch the connections between these services",
        ):
            self.assertTrue(requested(question), question)
        for question in (
            "Explain the engine without a diagram",
            "Create a picture of an engine",
            "Create an illustration of the engine",
            "Explain the engine",
        ):
            self.assertFalse(requested(question), question)
        with patch.dict(os.environ, {"ATLAS_STABLE": "0"}):
            self.assertTrue(
                explicit_image_request("Create a picture of a Stirling engine")
            )

    def test_picture_still_reaches_image_generation(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        async def image(
            prompt: str, item: Interaction, *args: object, **kwargs: object
        ) -> None:
            self.assertEqual(prompt, "Create a picture of a Stirling engine")
            item.images = [{"reference": "/images/scripted", "kind": "generated"}]
            item.reponse, item.state, item.task_type = (
                "![Engine](/images/scripted)",
                "done",
                "imagegen",
            )
            if sink := sink_context.get():
                await sink({"delta": {"content": item.reponse}})

        for stream in (False, True):
            with (
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    os.environ, {"ATLAS_STABLE": "0", "ATLAS_INTERACTION_DIR": root}
                ),
                patch(
                    "services.orchestrator.chat_pipeline.process_image",
                    side_effect=image,
                ) as generate,
                patch.object(GatewayModel, "connect", AsyncMock()) as text_model,
                TestClient(app) as client,
            ):
                payload = ChatRequest.model_validate(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Create a picture of a Stirling engine",
                            }
                        ],
                        "stream": stream,
                    }
                )
                response = client.post(
                    "/v1/chat/completions", json=payload.model_dump()
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("/images/scripted", response.text)
                generate.assert_awaited_once()
                text_model.assert_not_awaited()

    def test_only_unverified_image_gets_an_explicit_nonempty_answer(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                answer, _ = self.request(
                    stream, "Show the result", [("![invented](/images/missing)", False)]
                )
                self.assertIn("could not produce an answer", answer)
                self.assertNotIn("/images/missing", answer)


class TerminalTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_tool_return_keeps_all_prior_model_text(self) -> None:
        from services.orchestrator.loop import (
            Call,
            Limits,
            Query,
            Reservation,
            Turn,
            run,
        )
        from unittest.mock import Mock

        model = Mock()
        model.estimate.return_value = Reservation(10, Decimal("0"))
        model.complete = AsyncMock(
            side_effect=[
                Turn("First explanation. ", (Call("first", "calculator", "{}"),)),
                Turn(
                    "Then the diagram introduction.",
                    (Call("last", "generate_image", "{}"),),
                ),
            ]
        )
        tools = Mock()
        tools.estimate.return_value = Reservation(0, Decimal("0"))
        tools.execute = AsyncMock(return_value={"selected": True})
        result = await run(
            Query(question=STIRLING, lang="en"),
            model,
            tools,
            "",
            Limits(),
            terminal_tools=frozenset({"generate_image"}),
        )
        self.assertEqual(result.state, "done")
        self.assertEqual(
            result.text, "First explanation. Then the diagram introduction."
        )
        self.assertEqual(result.tool_calls, 2)

    def test_attachment_metadata_does_not_request_a_diagram(self) -> None:
        from services.orchestrator.diagram import requested

        metadata = '<attached_files><file name="diagram.pdf" url="/work/diagram/report.pdf" /></attached_files>\n'
        self.assertFalse(requested(metadata + "Summarize the attached report"))
        self.assertTrue(requested(metadata + "Explain the architecture with a diagram"))
        self.assertFalse(requested("[Attachment: diagram.pdf]\nSummarize this report"))
