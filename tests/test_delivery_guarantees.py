"""Task E: capture-derived refusals cannot substitute for delivered content."""

from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from agent_provider import AgentProvider, AgentRequest
from quality import stream_quality
from serverless_support import environment
from test_quality_gateway import request, result
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.terminal_client import TerminalClient

REPLIES = json.loads(Path("tests/fixtures/m26_delivery_replies.json").read_text())
CONTENT = (
    "Le fichier n'a pas pu être produit.\n"
    "1. Envoyez un ordre du jour précis avant la réunion.\n"
    "2. Désignez une personne pour noter les décisions.\n"
    "3. Attribuez chaque action à une personne avec une date limite."
)


class TerminalContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_captured_script_with_failure_admission_is_rejected(self) -> None:
        seen = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(req)
            text = REPLIES["J57"][-1] if len(seen) == 1 else CONTENT
            yield {"delta": {"content": text}}
            yield result(text, 25)

        original = request(
            messages=[
                {
                    "role": "user",
                    "content": "Crée un DOCX contenant trois conseils pour organiser une réunion.",
                },
                {"role": "tool", "content": '{"error":"terminal_operation_failed"}'},
            ],
            tools=[{"type": "function", "function": {"name": "terminal_command"}}],
            produces_files=True,
        )
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [e async for e in stream_quality(AgentProvider(), original)]
        final = events[-1]["result"]
        assert isinstance(final, dict)
        self.assertEqual(final["text"], CONTENT)
        self.assertEqual(len(seen), 2)
        self.assertEqual(
            "".join(
                str(delta.get("content", ""))
                for e in events
                if isinstance(delta := e.get("delta"), dict)
            ),
            CONTENT,
        )
        from packages.tool_history import final_messages

        for req in seen:
            self.assertEqual(req.tool_choice, "none")
            self.assertEqual(final_messages(req.messages)[-1]["role"], "system")
            self.assertIn("content", str(req.messages[-1]["content"]))

    async def test_second_noncontent_reply_fails_without_third_attempt(self) -> None:
        from packages.limits import ProviderFailure

        seen = []
        visible = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(req)
            yield {"delta": {"content": REPLIES["J57"][-1]}}
            yield result(REPLIES["J57"][-1], 25)

        original = request(
            messages=[
                {"role": "user", "content": "Create a DOCX with three meeting tips."},
                {"role": "tool", "content": '{"error":"terminal_operation_failed"}'},
            ],
            produces_files=True,
        )
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
            self.assertRaises(ProviderFailure),
        ):
            async for event in stream_quality(AgentProvider(), original):
                if "delta" in event:
                    visible.append(event)
        self.assertEqual(len(seen), 2)
        self.assertEqual(visible, [])
        self.assertGreater(
            len(str(seen[1].messages[-1]["content"])),
            len(str(seen[0].messages[-1]["content"])),
        )


class FileGuaranteeTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_presentation_outline_publishes_all_requested_files(
        self,
    ) -> None:
        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )

        async def runner(*args: object, **kwargs: object) -> Result:
            return Result(
                text=(
                    '{"title":"/e/OS et Murena","slides":['
                    '{"title":"/e/OS","bullets":["/e/OS protège la vie privée."]},'
                    '{"title":"Murena","bullets":["Murena propose des appareils."]}'
                    "]}"
                ),
                state="done",
            )

        async def publish(
            client: TerminalClient, path: str, strategy: str, timeout: float
        ) -> dict[str, object]:
            url = "/api/v1/terminals/atlas-files/files/serve/request/" + path
            client.files.append(url)
            client.strategies.append(strategy)
            return {"download_url": url}

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_TERMINAL_ENABLED": "1",
                    "ATLAS_WEB_CACHE": root + "/cache",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(TerminalClient, "initialize", AsyncMock()),
            patch.object(
                TerminalClient,
                "upload_generated",
                AsyncMock(
                    side_effect=[
                        "/request/presentation.pptx",
                        "/request/presentation.pdf",
                    ]
                ),
            ) as upload,
            patch.object(TerminalClient, "publish", publish),
            patch("services.orchestrator.chat_pipeline.run", runner),
        ):
            item = Interaction()
            await process(
                ChatRequest.model_validate(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Crée une présentation PPTX et PDF sur /e/OS et Murena.",
                            }
                        ]
                    }
                ),
                item,
            )
        self.assertEqual(len(item.files), 2)
        self.assertEqual(upload.await_count, 2)
        self.assertFalse(
            any(
                isinstance(tool, dict)
                and isinstance(function := tool.get("function"), dict)
                and function.get("name") in {"terminal_command", "publish_document"}
                for tool in model.tools
            )
        )
        self.assertNotIn("Aucun fichier", item.reponse)

    def test_unpublished_original_claims_are_removed(self) -> None:
        from services.orchestrator.file_results import inline_fallback

        text = inline_fallback(REPLIES["J54"][0], "fr")
        self.assertNotIn("liens pour télécharger", text)
        self.assertNotIn("Je publie maintenant", text)
        self.assertNotIn("untrusted_tool_history", text)
        self.assertIn("microG", text)
        self.assertIn("Aucun fichier", text)


class FileStreamGuaranteeTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_drafts_are_not_visible_before_publication(self) -> None:
        import time
        from services.orchestrator.chat_stream import response
        from services.orchestrator.stream_client import sink_context
        from services.orchestrator.file_results import inline_fallback

        async def draft(query: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"content": REPLIES["J54"][0]}})
            item.reponse = inline_fallback(REPLIES["J54"][0], "fr")
            item.state = "done"

        query = ChatRequest.model_validate(
            {
                "stream": True,
                "messages": [
                    {"role": "user", "content": "Crée une présentation PPTX et PDF."}
                ],
            }
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            events = [
                json.loads(str(e)[6:])
                async for e in response(
                    query, Interaction(), time.monotonic(), draft
                ).body_iterator
                if str(e).startswith("data: {")
            ]
        text = "".join(
            e.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for e in events
        )
        self.assertNotIn("liens pour télécharger", text)
        self.assertNotIn("Je publie maintenant", text)
        self.assertIn("Aucun fichier", text)
        self.assertIn("microG", text)


class ContentValidationTests(unittest.TestCase):
    def test_failure_content_is_not_an_apology_intention_or_script(self) -> None:
        from answer_content import failed_terminal_content

        messages: list[dict[str, object]] = [
            {"role": "user", "content": "Give three meeting tips in a DOCX."}
        ]
        for text in (
            REPLIES["J57"][-1],
            "The file operation failed. I will create it later.",
            "Sorry, the file could not be produced.",
            "The file operation failed.\nfrom docx import Document\ndoc = Document()",
        ):
            self.assertTrue(failed_terminal_content(text, messages), text)
        self.assertFalse(failed_terminal_content(CONTENT, messages))
        self.assertFalse(
            failed_terminal_content(
                "The file operation failed.\n```python\ndef median(xs):\n    return sorted(xs)[len(xs)//2]\n```",
                [{"role": "user", "content": "Write a Python script in a file."}],
            )
        )

    def test_renderer_preserves_sections_without_padding_or_invented_language(
        self,
    ) -> None:
        from services.orchestrator.file_content import render_command, sections

        for text in (
            "# Alltag\n- Glas getrennt sammeln.\n- Papier trocken lagern.",
            "# Résumé\n- Trier le verre.",
            "# Resumen\n- Separar el vidrio.",
        ):
            slides = sections(text)
            self.assertEqual(len(slides), 1)
            self.assertTrue(slides[0][1])
            self.assertEqual(slides[0][0], text.splitlines()[0][2:])
            command, paths = render_command(text, ("pdf",))
            self.assertEqual(paths, ["answer.pdf"])
            self.assertIn(slides[0][0], command)
        self.assertEqual(
            render_command(
                "Je vais créer le fichier.\n```python\ncreate()\n```", ("pptx", "pdf")
            ),
            ("", []),
        )

    def test_failure_disclosure_survives_but_is_never_rendered_as_a_slide(self) -> None:
        from services.orchestrator.file_content import answer_content, sections

        for text in (
            "Le fichier n'a pas pu être généré.",
            "The file could not be created.",
            "Keine Datei konnte veröffentlicht werden.",
        ):
            self.assertEqual(answer_content(text), text)
            self.assertEqual(sections(text), [])

    def test_required_tool_choice_has_a_declared_file_tool(self) -> None:
        from pydantic import ValidationError

        for name in ("terminal_command", "publish_document"):
            choice = {"type": "function", "function": {"name": name}}
            req = request(
                tools=[{"type": "function", "function": {"name": name}}],
                tool_choice=choice,
            )
            self.assertEqual(req.model_dump()["tool_choice"], choice)
            with self.assertRaises(ValidationError):
                request(tool_choice=choice, tools=[])


class RequiredToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_requires_publication_until_all_formats_exist(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.loop import Call

        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        model.required_file_tool = "terminal_command"
        model.required_file_formats = ("pptx", "pdf")

        async def publish(
            client: TerminalClient, path: str, strategy: str, timeout: float
        ) -> dict[str, object]:
            client.files.append("/files/" + path)
            client.strategies.append(strategy)
            return {"download_url": client.files[-1]}

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {"ATLAS_TERMINAL_ENABLED": "1", "ATLAS_WEB_CACHE": root + "/cache"},
            ),
            patch.object(
                TerminalClient, "execute", AsyncMock(return_value={"exit_code": 0})
            ),
            patch.object(TerminalClient, "publish", publish),
        ):
            tools = ChatTools(Interaction())
            tools.model = model
            await tools.execute(
                Call("create", "terminal_command", '{"command":"build"}'), 5
            )
            self.assertEqual(model.required_file_tool, "publish_document")
            await tools.execute(
                Call(
                    "pptx",
                    "publish_document",
                    '{"path":"answer.pptx","strategy":"created"}',
                ),
                5,
            )
            self.assertEqual(model.required_file_tool, "publish_document")
            await tools.execute(
                Call(
                    "pdf",
                    "publish_document",
                    '{"path":"answer.pdf","strategy":"created"}',
                ),
                5,
            )
            self.assertIsNone(model.required_file_tool)

    async def test_required_choice_reaches_gateway_payload(self) -> None:
        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        model.tools = [{"type": "function", "function": {"name": "terminal_command"}}]
        model.required_file_tool = "terminal_command"
        reply = {
            "text": "",
            "calls": [
                {
                    "id": "create",
                    "name": "terminal_command",
                    "arguments": '{"command":"build"}',
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
        with patch.object(model, "post", AsyncMock(return_value=reply)) as post:
            await model.complete([{"role": "user", "content": "Create a PPTX."}], 5)
        assert post.await_args is not None
        self.assertEqual(
            post.await_args.args[1]["tool_choice"],
            {"type": "function", "function": {"name": "terminal_command"}},
        )
