"""D7 shopping regressions: search retrieval survives blocked shop fetches."""

import json
import tempfile
import unittest
from typing import cast
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.link_requests import page_request, product_queries
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.searxng import parse_results
from services.orchestrator.shopping_prices import verified_prices


class ShoppingPolicyTests(unittest.TestCase):
    def test_product_shop_queries_do_not_echo_conversation(self) -> None:
        queries = product_queries(
            "Donne des liens chez thomann ou autre",
            ("Budget 500 euros", "Yamaha P-145 et Roland FP-30X."),
        )
        self.assertEqual(len(queries), 2)
        for model, query in zip(("Yamaha P-145", "Roland FP-30X"), queries):
            self.assertTrue(query.startswith(model))
            self.assertIn("site:thomann.de", query)
            self.assertIn("site:woodbrass.com", query)
            self.assertNotIn("Donne", query)

    def test_only_related_followups_inherit_link_route(self) -> None:
        history = ("Where can I buy these?", "Yamaha P-145")
        self.assertTrue(page_request("What does it cost?", history))
        self.assertTrue(page_request("Et chez Woodbrass ?", history))
        self.assertFalse(page_request("What does it cost?"))
        self.assertFalse(page_request("What does it cost?", ("Explain IPOs",)))
        self.assertFalse(page_request("No links please", history))

    def test_snippet_price_has_source_and_reading_qualifier(self) -> None:
        url = "https://shop.example/piano"
        text = "- [Shop](" + url + ") — 499 EUR"
        answer = verified_prices(text, {}, "en", {url: "Piano 499 EUR"})
        self.assertIn("499 EUR (price at the time of reading)", answer)
        self.assertIn("source: search snippet — shop.example", answer)
        self.assertNotIn(
            "499", verified_prices(text, {}, "en", {"https://other.example": "499 EUR"})
        )

    def test_euro_word_is_not_partially_replaced(self) -> None:
        answer = verified_prices("Budget 500 euros", {}, "fr")
        self.assertNotIn("non vérifiéos", answer)


class BlockedShopPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_searxng_snippets_remain_usable_when_fetch_is_blocked(self) -> None:
        first = "https://shop.example/yamaha-p145"
        second = "https://other.example/yamaha-p145"
        results = parse_results(
            {
                "results": [
                    {
                        "url": first,
                        "title": "Yamaha P-145",
                        "content": "Yamaha P-145: 499 EUR",
                        "engine": "google",
                    },
                    {
                        "url": second,
                        "title": "Yamaha P-145",
                        "content": "Yamaha P-145 digital piano",
                        "engine": "google",
                    },
                ]
            },
            "google",
        )
        results.update(provider="searxng", outcome="partial_results")
        model = GatewayModel(
            "http://unused.invalid",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=1000,
            ),
        )
        answer = (
            f"Yamaha P-145\n- [Shop]({first}) — 499 EUR\n- [Other]({second}) — 399 EUR"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_WEB_CACHE": directory + "/cache",
                    "ATLAS_TERMINAL_ENABLED": "0",
                },
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch.object(
                model,
                "complete",
                new=AsyncMock(
                    return_value=Turn(answer, usage=Reservation(0, Decimal(0)))
                ),
            ),
            patch(
                "services.orchestrator.tools.Runtime.search",
                new=AsyncMock(return_value=results),
            ) as search,
            patch(
                "services.orchestrator.tools.Runtime.fetch",
                new=AsyncMock(side_effect=ValueError("robots_denied")),
            ) as fetch,
        ):
            item = Interaction()
            await process(
                ChatRequest.model_validate(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Product links for Yamaha P-145 at shops",
                            }
                        ],
                        "ui_locale": "en",
                    }
                ),
                item,
            )
        self.assertEqual(item.state, "done")
        self.assertIn(f"[Shop]({first})", item.reponse)
        self.assertIn(f"[Other]({second})", item.reponse)
        self.assertIn("499 EUR (price at the time of reading)", item.reponse)
        self.assertIn("source: search snippet", item.reponse)
        self.assertNotIn("399", item.reponse)
        self.assertIn("price not verified", item.reponse)
        self.assertIn("site:thomann.de", search.call_args.args[0].query)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(set(item.retrieved_urls), {first, second})


class RecordedShoppingTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_shop_results_survive_blocked_fetch_and_model_omission(
        self,
    ) -> None:
        from pathlib import Path
        from services.orchestrator.source_truth import urls

        recorded = json.loads(
            Path("tests/fixtures/links-shopping-searxng.json").read_text()
        )
        searches = {
            row["request"]["query"]: row["response"]
            for row in recorded["rows"]
            if row["kind"] == "search"
        }

        async def search(request: object, timeout: float) -> dict[str, object]:
            return cast(dict[str, object], searches[getattr(request, "query")])

        model = GatewayModel(
            "http://unused.invalid",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=1000,
            ),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_WEB_CACHE": directory + "/cache",
                    "ATLAS_TERMINAL_ENABLED": "0",
                },
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch.object(
                model,
                "complete",
                new=AsyncMock(
                    return_value=Turn(
                        "- Woodbrass :\n  -\n- Boulanger :",
                        usage=Reservation(0, Decimal(0)),
                    )
                ),
            ),
            patch(
                "services.orchestrator.tools.Runtime.search",
                new=AsyncMock(side_effect=search),
            ),
            patch(
                "services.orchestrator.tools.Runtime.fetch",
                new=AsyncMock(side_effect=ValueError("robots_denied")),
            ),
        ):
            item = Interaction()
            await process(
                ChatRequest.model_validate(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Quels pianos numériques me conseilles-tu pour débuter, budget 500 euros ?",
                            },
                            {
                                "role": "assistant",
                                "content": "Yamaha P-145, Roland FP-10, Casio CT-S1.",
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
        prose = item.reponse.split("<details>")[0]
        self.assertEqual(len(urls(prose)), 4)
        for shop in ("Woodbrass", "Thomann", "Fnac"):
            self.assertIn("[" + shop + "](", prose)
        self.assertIn("Casio CT-S1\nJe n’ai pas pu récupérer de liens vérifiés", prose)
        self.assertIn("Termes à rechercher", prose)
        self.assertNotIn("Woodbrass :", prose)
        self.assertNotIn("Boulanger :", prose)
        self.assertNotRegex(prose, r"(?m)^\s*[-*+]\s*$")
        retrieved = {
            row["link"]
            for response in searches.values()
            for row in response.get("results", [])
        }
        self.assertTrue(urls(prose).issubset(retrieved))
