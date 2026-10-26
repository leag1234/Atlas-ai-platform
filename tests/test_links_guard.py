"""Owner defect 2026-10-25: empty shop bullets after a piano follow-up."""

import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.citation_cleanup import cleanup, hollow
from services.orchestrator.link_requests import link_request
from services.orchestrator.loop import Call, Limits, Query, Reservation, Turn, run
from services.orchestrator.search_policy import required_research, requested_page_read
from services.orchestrator.source_truth import without_unretrieved

# Verbatim structure reported by the owner, represented as Markdown lines.
OBSERVED = """1. Yamaha P-45 / P-145
   - Thomann :
     -
   - Woodbrass :
     -
2. Roland FP-10 / FP-30X
   - Thomann :
     -
   - Boulanger :
     -
"""


class LinksGuardTests(unittest.TestCase):
    def test_owner_empty_structure(self) -> None:
        self.assertEqual(cleanup(OBSERVED), "")
        self.assertTrue(hollow(OBSERVED, cleanup(OBSERVED)))

    def test_removed_links_empty_sections_and_setext(self) -> None:
        raw = OBSERVED.replace(
            "     -\n", "     - [page](https://invalid.example/piano)\n"
        )
        self.assertEqual(without_unretrieved(raw, set()), "")
        self.assertEqual(
            without_unretrieved(
                "Thomann :\n[page](https://invalid.example/piano)\n---\n\nUseful explanation.",
                set(),
            ),
            "Useful explanation.",
        )

    def test_keep_useful_content_and_code(self) -> None:
        raw = "## Yamaha\n- Thomann : [page](https://shop.example/piano)\n- Other : [page](https://invalid.example/piano)"
        answer = without_unretrieved(raw, {"https://shop.example/piano"})
        self.assertIn("Thomann : [page](https://shop.example/piano)", answer)
        self.assertNotIn("Other", answer)
        code = "```python\nlabel = 'Thomann :'\n---\n```"
        self.assertEqual(cleanup(code), code)

    def test_empty_peer_is_removed_but_valid_label_and_nested_heading_survive(
        self,
    ) -> None:
        text = "## Piano\n### Shops\n- Thomann :\n- Woodbrass : [page](https://shop.example)"
        answer = cleanup(text)
        self.assertNotIn("Thomann", answer)
        self.assertIn("## Piano", answer)
        self.assertIn("### Shops", answer)
        self.assertIn("Woodbrass", answer)
        self.assertEqual(
            cleanup("Thomann :\n[page](https://shop.example)"),
            "Thomann :\n[page](https://shop.example)",
        )

    def test_standalone_prices_keep_previous_route(self) -> None:
        for question in [
            "Quel prix ?",
            "Current price?",
            "Preise?",
            "Combien ça coûte ?",
            "Combien pour ces modèles ?",
            "How much for those?",
            "What does it cost?",
            "Was kostet das?",
            "Wie teuer?",
        ]:
            with self.subTest(question=question):
                self.assertFalse(requested_page_read(question))
                self.assertFalse(link_request(question))
        calls = required_research("What was the initial offer price?", "en")
        self.assertEqual(calls[0].id, "required-current")

    def test_numbered_instructions_with_inline_bold_are_not_empty_headings(
        self,
    ) -> None:
        text = (
            "3. Open the camera.\n"
            "4. **No chemicals:** Unlike traditional photography, do not develop the paper.\n"
            "5. Scan the negative."
        )
        self.assertEqual(cleanup(text), text)

    def test_recorded_j36_instruction_survives_citation_cleanup(self) -> None:
        import gzip
        from pathlib import Path

        rows = json.loads(
            gzip.decompress(Path("tests/cassettes/m22.json.gz").read_bytes())
        )
        text = "".join(
            event.get("event", {}).get("delta", {}).get("content", "")
            for event in rows[20]["response"]
        )
        answer = without_unretrieved(text, set())
        self.assertIn("Contrairement à la photographie traditionnelle", answer)
        self.assertIn("on ne développe pas ce papier", answer)

    def test_multilingual_route(self) -> None:
        for question, lang in (
            ("Donne des liens chez thomann ou autre", "fr"),
            ("des liens pour ces modèles", "fr"),
            ("OU LES ACHETER", "fr"),
            ("Montre la page", "fr"),
            ("fiche produit", "fr"),
            ("Show me the page", "en"),
            ("Links for those", "en"),
            ("Where can I get those?", "en"),
            ("product pages", "en"),
            ("Wo bekomme ich diese?", "de"),
            ("Links bitte", "de"),
            ("Zeig mir die Seite", "de"),
            ("Wo kaufen?", "de"),
        ):
            with self.subTest(question=question):
                self.assertTrue(link_request(question))
                self.assertTrue(requested_page_read(question))
                call = required_research(question, lang, history=("Yamaha P-145",))[0]
                self.assertEqual(call.name, "web_search")
                self.assertIn("Yamaha P-145", json.loads(call.arguments)["query"])
        for question in (
            "Sans liens",
            "No links please",
            "Keine Links",
            "Explain a triangle",
            "Internal price",
            '<attached_files><file type="filesystem" url="/tmp/price.pdf" /></attached_files> Résume ces pièces jointes.',
        ):
            self.assertFalse(link_request(question))


class LinksJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_keeps_remote_fetch_project_file_link(self) -> None:
        url = "https://atlas.example/api/atlas/project/files/report.txt/download"
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(
                side_effect=[
                    Turn("", (Call("fetch", "remote_fetch", "{}"),)),
                    Turn(f"Remote checks completed. [Download report]({url})"),
                ]
            ),
        )
        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal(0))),
            execute=AsyncMock(return_value={"download_url": url}),
        )
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="Give me the project file link", lang="en"),
                model,
                tools,
                "",
                Limits(
                    profile="atlas-qwen",
                    tokens=None,
                    tool_calls=2,
                    wall_clock=30,
                    cost=Decimal("1"),
                    model_calls=2,
                    code_mode=True,
                ),
            )
        self.assertEqual(result.state, "done")
        self.assertIn("Remote checks completed.", result.text)
        self.assertIn(url, result.text)
        self.assertNotIn("could not retrieve verified links", result.text)

    async def test_work_removes_only_an_invented_external_link(self) -> None:
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(
                return_value=Turn(
                    "Remote checks completed. [Invented](https://store.example/item)"
                )
            ),
        )
        tools = Mock(estimate=Mock(return_value=Reservation(0, Decimal(0))))
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="Give me the project file link", lang="en"),
                model,
                tools,
                "",
                Limits(
                    profile="atlas-qwen",
                    tokens=None,
                    tool_calls=1,
                    wall_clock=30,
                    cost=Decimal("1"),
                    model_calls=1,
                    code_mode=True,
                ),
            )
        self.assertEqual(result.state, "done")
        self.assertIn("Remote checks completed.", result.text)
        self.assertNotIn("store.example", result.text)
        self.assertNotIn("could not retrieve verified links", result.text)

    async def test_owner_reply_recovers_with_same_budget(self) -> None:
        answer = "Yamaha P-145 — Thomann : [product page](https://shop.example/piano). Price could not be verified."
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(side_effect=[Turn(OBSERVED), Turn(answer)]),
        )

        async def execute(call: object, timeout: float) -> dict[str, object]:
            return {
                "data": {
                    "results": [{"link": "https://shop.example/piano"}],
                    "url": "https://shop.example/piano",
                    "text": "Yamaha P-145",
                }
            }

        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal("0.001"))),
            execute=AsyncMock(side_effect=execute),
        )
        result = await run(
            Query(question="Donne des liens chez thomann ou autre", lang="fr"),
            model,
            tools,
            "",
            Limits(),
            history=[{"role": "assistant", "content": "Yamaha P-145"}],
        )
        self.assertEqual(result.state, "done")
        self.assertIn("https://shop.example/piano", result.text)
        self.assertIn("Yamaha", result.text)
        self.assertEqual(
            [c.args[0].name for c in tools.execute.call_args_list],
            ["web_search", "web_fetch"],
        )
        self.assertLessEqual(result.cost, Decimal("0.05"))

    async def test_recovery_cannot_exceed_tool_budget(self) -> None:
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(return_value=Turn(OBSERVED)),
        )
        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal("0.001"))),
            execute=AsyncMock(),
        )
        result = await run(
            Query(question="Donne des liens chez thomann ou autre", lang="fr"),
            model,
            tools,
            "",
            Limits(tool_calls=0),
        )
        self.assertEqual(result.reason, "tool_calls")
        self.assertIn("liens vérifiés", result.text)
        self.assertIn("limit 0", result.text)
        tools.execute.assert_not_awaited()

    async def test_failed_search_yields_terms_not_hollow_shops(self) -> None:
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(return_value=Turn(OBSERVED)),
        )
        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal("0.001"))),
            execute=AsyncMock(return_value={"error": "unavailable"}),
        )
        result = await run(
            Query(question="Donne des liens chez thomann ou autre", lang="fr"),
            model,
            tools,
            "",
        )
        self.assertIn("liens vérifiés", result.text)
        self.assertIn("Termes", result.text)
        self.assertNotIn("Thomann :", result.text)
        self.assertEqual(tools.execute.await_count, 1)


class PriceEvidenceTests(unittest.TestCase):
    def test_prices_are_bound_to_fetched_link_not_snippets_or_other_shops(self) -> None:
        from services.orchestrator.shopping_prices import verified_prices

        text = "- Thomann: [page](https://shop.example/piano) — 499 €\n- Other: [page](https://other.example/piano) — 499 €\n- Invented: [page](https://shop.example/piano) — 299 €"
        answer = verified_prices(
            text, {"https://shop.example/piano": "Piano: EUR 499.00"}, "fr"
        )
        self.assertIn("499 € (prix au moment de la lecture)", answer)
        self.assertEqual(answer.count("499"), 1)
        self.assertNotIn("299", answer)
        self.assertIn("Other", answer)
        self.assertIn("prix non vérifié", answer)

    def test_unlinked_prices_and_locale_formats(self) -> None:
        from services.orchestrator.shopping_prices import verified_prices

        answer = verified_prices(
            "[page](https://shop.example) 1.299,00 EUR",
            {"https://shop.example": "€1,299.00"},
            "de",
        )
        self.assertIn("1.299,00 EUR (Preis zum Zeitpunkt des Abrufs)", answer)
        self.assertNotIn("499", verified_prices("Price: $499", {}, "en"))


class FollowupPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_offers_tools_reads_pages_and_keeps_shop_name(self) -> None:
        import tempfile
        from unittest.mock import patch
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import Configuration, GatewayModel
        from services.orchestrator.loop import Message
        from services.orchestrator.stream_client import sink_context

        captured: list[Message] = []
        offered: list[dict[str, object]] = []

        class ScriptedModel(GatewayModel):
            async def complete(self, messages: list[Message], timeout: float) -> Turn:
                captured.extend(messages)
                offered.extend(self.tools)
                return Turn(
                    "Yamaha P-145\n- [Thomann](https://shop.example/piano): 499 €",
                    usage=Reservation(0, Decimal(0)),
                )

        model = ScriptedModel(
            "http://unused.invalid",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=1000,
            ),
        )
        events: list[Message] = []

        async def sink(event: Message) -> None:
            events.append(event)

        token = sink_context.set(sink)
        try:
            with (
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    "os.environ",
                    {"ATLAS_WEB_CACHE": root + "/cache", "ATLAS_TERMINAL_ENABLED": "0"},
                ),
                patch(
                    "services.orchestrator.chat_pipeline.GatewayModel.connect",
                    new=AsyncMock(return_value=model),
                ),
                patch(
                    "services.orchestrator.tools.Runtime.search",
                    new=AsyncMock(
                        return_value={
                            "results": [{"link": "https://shop.example/piano"}]
                        }
                    ),
                ),
                patch(
                    "services.orchestrator.tools.Runtime.fetch",
                    new=AsyncMock(
                        return_value={
                            "url": "https://shop.example/piano",
                            "text": "Yamaha P-145 — 499 EUR",
                        }
                    ),
                ),
            ):
                item = Interaction()
                await process(
                    ChatRequest.model_validate(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Recommande des pianos numériques",
                                },
                                {
                                    "role": "assistant",
                                    "content": "Yamaha P-145 et Roland FP-10.",
                                },
                                {
                                    "role": "user",
                                    "content": "Donne des liens chez thomann ou autre",
                                },
                            ],
                            "ui_locale": "fr",
                        }
                    ),
                    item,
                )
            self.assertEqual(item.state, "done")
            self.assertIn("[Thomann](https://shop.example/piano)", item.reponse)
            self.assertIn("499 € (prix au moment de la lecture)", item.reponse)
            self.assertIn("web_search", str(offered))
            self.assertIn("web_fetch", str(offered))
            self.assertIn("in that URL's search snippet", str(captured[0]["content"]))
            self.assertEqual(
                [e["tool"] for e in events if e.get("phase") == "tool_started"],
                ["web_search", "web_search", "web_fetch", "web_fetch"],
            )
        finally:
            sink_context.reset(token)


class OwnerStreamJourneyTests(unittest.TestCase):
    def test_owner_observation_through_public_stream(self) -> None:
        import tempfile
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app
        from services.orchestrator.model import Configuration, GatewayModel

        model = GatewayModel(
            "http://unused.invalid",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=1000,
            ),
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_WEB_CACHE": root + "/cache",
                    "ATLAS_INTERACTION_DIR": root + "/logs",
                    "ATLAS_TERMINAL_ENABLED": "0",
                },
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch.object(model, "complete", new=AsyncMock(return_value=Turn(OBSERVED))),
            patch(
                "services.orchestrator.tools.Runtime.search",
                new=AsyncMock(
                    return_value={"results": [{"link": "https://shop.example/piano"}]}
                ),
            ),
            patch(
                "services.orchestrator.tools.Runtime.fetch",
                new=AsyncMock(
                    return_value={
                        "url": "https://shop.example/piano",
                        "text": "Yamaha P-145",
                    }
                ),
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "stream": True,
                    "messages": [
                        {"role": "user", "content": "Recommande des pianos numériques"},
                        {
                            "role": "assistant",
                            "content": "Yamaha P-145 et Roland FP-10.",
                        },
                        {
                            "role": "user",
                            "content": "Donne des liens chez thomann ou autre",
                        },
                    ],
                    "ui_locale": "fr",
                },
            )
        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        answer = "".join(
            choice.get("delta", {}).get("content", "")
            for event in events
            for choice in event.get("choices", [])
        )
        self.assertIn("web_search", response.text)
        self.assertIn("web_fetch", response.text)
        self.assertIn("liens vérifiés", answer)
        self.assertIn("Yamaha P-145", answer)
        self.assertNotIn("Thomann :", answer)
        self.assertNotIn("Woodbrass :", answer)
        self.assertNotRegex(answer, r"(?m)^\s*[-*+]\s*$")


class HistoricalPricePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_amounts_keep_separate_retrieved_citation(self) -> None:
        import tempfile
        from unittest.mock import patch
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import Configuration, GatewayModel

        source = "https://source.example/history"
        text = (
            "The offer price in 2026 was 135 USD; the opening price was 150 USD.\n\n[History]("
            + source
            + ")"
        )
        model = GatewayModel(
            "http://unused.invalid",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=1000,
            ),
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                "os.environ",
                {"ATLAS_WEB_CACHE": root + "/cache", "ATLAS_TERMINAL_ENABLED": "0"},
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch.object(model, "complete", new=AsyncMock(return_value=Turn(text))),
            patch(
                "services.orchestrator.tools.Runtime.search",
                new=AsyncMock(return_value={"results": [{"link": source}]}),
            ),
            patch(
                "services.orchestrator.tools.Runtime.fetch",
                new=AsyncMock(
                    return_value={
                        "url": source,
                        "text": "Offer 135 USD; opening 150 USD.",
                    }
                ),
            ),
        ):
            item = Interaction()
            await process(
                ChatRequest.model_validate(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "What was the initial offer price?",
                            }
                        ],
                        "ui_locale": "en",
                    }
                ),
                item,
            )
        self.assertEqual(item.state, "done")
        self.assertIn("135 USD", item.reponse)
        self.assertIn("150 USD", item.reponse)
        self.assertNotIn("price not verified", item.reponse)
