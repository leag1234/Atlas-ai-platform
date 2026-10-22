"""M7 preserves web/calculator guards and accounts for retrieval failures."""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call


class ChatToolTests(unittest.IsolatedAsyncioTestCase):
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
