"""M7 preserves web/calculator guards and accounts for retrieval failures."""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import (
    ChatTools,
    partial_search_failure_notice,
)
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call


class ChatToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_results_are_verified_before_a_later_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
        ):
            item = Interaction()
            tools = ChatTools(item)
            successful = {
                "trust": "untrusted",
                "data": {"results": [{"link": "https://example.test/verified"}]},
            }
            failed = {
                "trust": "untrusted",
                "data": {"outcome": "upstream_blocked", "results": []},
            }
            with patch(
                "services.orchestrator.tools.Runtime.execute",
                new=AsyncMock(side_effect=[successful, failed]),
            ):
                await tools.execute(
                    Call("1", "web_search", '{"query":"first","lang":"en"}'), 1
                )
                result = await tools.execute(
                    Call("2", "web_search", '{"query":"later","lang":"en"}'), 1
                )
        self.assertEqual(item.retrieved_urls, ["https://example.test/verified"])
        self.assertEqual(result, failed)
        self.assertEqual(
            item.erreurs,
            ["upstream_blocked", "partial_search_failure:upstream_blocked"],
        )
        for language in ("en", "fr", "de", "es", "it"):
            with self.subTest(language=language):
                notice = partial_search_failure_notice("upstream_blocked", language)
                self.assertIn("upstream_blocked", notice)
                self.assertNotIn("no verified results were obtained", notice.lower())

    async def test_delegation_and_retrieval_failures(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
        ):
            item = Interaction()
            tools = ChatTools(item)
            result = await tools.execute(Call("1", "calculator", '{"expr":"1+1"}'), 1)
            self.assertEqual(str(result["value"]), "2")
            with patch(
                "services.orchestrator.tools.Runtime.execute",
                new=AsyncMock(return_value={"error": "ssrf_blocked"}),
            ) as execute:
                call = Call("2", "web_fetch", '{"url":"http://127.0.0.1"}')
                self.assertEqual(
                    await tools.execute(call, 1), {"error": "ssrf_blocked"}
                )
                execute.assert_awaited_once_with(call, 1)
            for arguments in ("{", '{"query":"source"}'):
                with patch(
                    "services.orchestrator.chat_pipeline.GatewayModel.post",
                    new=AsyncMock(side_effect=TimeoutError),
                ):
                    self.assertEqual(
                        await tools.execute(Call("3", "rag_search", arguments), 1),
                        {"error": "retrieval_unavailable"},
                    )
            self.assertEqual(
                item.erreurs,
                ["ssrf_blocked", "retrieval_unavailable", "retrieval_unavailable"],
            )
            self.assertGreater(item.latence_ms["retrieval"], 0)
            self.assertEqual(item.chunks_recuperes, [])
