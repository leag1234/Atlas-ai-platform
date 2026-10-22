"""Cache isolation, provider errors and strict tool arguments, POC-W1/W2."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.orchestrator.cache import Cache
from services.orchestrator.loop import Call
from services.orchestrator.tools import Fetch, Runtime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Cache(Path(directory.name) / "cache.sqlite")
        self.runtime = Runtime(self.cache, Decimal(0))

    async def test_invalid_tool_arguments(self) -> None:
        for name, args in [
            ("missing", {}),
            ("calculator", {"expr": "1", "extra": True}),
            ("web_search", {"query": " ", "lang": "fr"}),
            ("web_search", {"query": "news", "lang": "xx"}),
            ("web_search", {"query": "news", "lang": "fr", "n": 6}),
        ]:
            result = await self.runtime.execute(Call("1", name, json.dumps(args)), 1)
            self.assertIn("error", result)
        result = await self.runtime.execute(
            Call("1", "calculator", '{"expr":"1.25*2"}'), 1
        )
        self.assertEqual(result, {"value": "2.50"})

    async def test_ssrf_via_runtime(self) -> None:
        result = await self.runtime.execute(
            Call("1", "web_fetch", '{"url":"http://127.0.0.1"}'), 1
        )
        self.assertEqual(result, {"error": "ssrf_denied"})

    async def test_cache_extraction_continuation(self) -> None:
        text = "Public evidence. " * 1000
        with patch.object(
            self.runtime.web,
            "fetch",
            new=AsyncMock(return_value=("https://example.org", b"html")),
        ) as fetch:
            with patch("trafilatura.extract", return_value=text):
                first = await self.runtime.fetch(Fetch(url="https://example.org"), 1)
                second = await self.runtime.fetch(Fetch(url="https://example.org"), 1)
        self.assertEqual(first, second)
        self.assertEqual(fetch.await_count, 1)
        self.assertLessEqual(len(str(first["text"]).encode()), 8000)
        rest = self.cache.get(str(first["handle"]))
        self.assertIsNotNone(rest)
        assert rest is not None
        self.assertEqual(rest["text"], text)
        self.assertTrue(first["selected"])
        synthesis = first["synthesis"]
        assert isinstance(synthesis, dict)
        self.assertEqual(synthesis["method"], "hierarchical_extractive")
        self.assertEqual(synthesis["source_characters"], len(text))
        self.assertIn("consulted_at", first)

    async def test_timeout_and_extraction_error(self) -> None:
        with patch.object(
            self.runtime, "fetch", new=AsyncMock(side_effect=TimeoutError)
        ):
            result = await self.runtime.execute(
                Call("1", "web_fetch", '{"url":"https://example.org"}'), 1
            )
            self.assertEqual(result, {"error": "timeout"})
        with patch.object(
            self.runtime.web,
            "fetch",
            new=AsyncMock(return_value=("https://example.org", b"")),
        ):
            with patch("trafilatura.extract", return_value=None):
                with self.assertRaisesRegex(ValueError, "extraction_empty"):
                    await self.runtime.fetch(Fetch(url="https://example.org"), 1)

    async def test_search_localization_cache_and_preserved_url(self) -> None:
        from collections.abc import AsyncIterator
        from unittest.mock import MagicMock
        from services.orchestrator.tools import Search

        link = "https://example.org/" + "a" * 400

        async def pieces(size: int) -> AsyncIterator[bytes]:
            yield json.dumps(
                {
                    "organic_results": [
                        {"title": "News", "link": link, "snippet": "evidence"}
                    ]
                }
            ).encode()

        response = MagicMock(status=200)
        response.content.iter_chunked = pieces
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.get.return_value = context
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        with (
            patch.dict("os.environ", {"SERPAPI_KEY": "test-only"}),
            patch(
                "services.orchestrator.tools.aiohttp.ClientSession", return_value=client
            ),
        ):
            request = Search(query="public news", lang="de")
            first = await self.runtime.search(request, 1)
            second = await self.runtime.search(request, 1)
        self.assertEqual(first, second)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.get.call_args.kwargs["params"]["gl"], "de")
        self.assertEqual(session.get.call_args.kwargs["params"]["hl"], "de")
        self.assertIn(link, json.dumps(first))
