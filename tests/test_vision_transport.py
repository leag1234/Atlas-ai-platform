"""M12 provider transport failures stay bounded and redact provider details."""

from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import aiohttp
from vision import VisionProvider


class VisionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_transport_and_failure_boundaries(self) -> None:
        recorded = json.loads(
            Path("tests/cassettes/vision/response-0.json").read_text()
        )["response"]
        for status, data, succeeds in (
            (200, json.dumps(recorded).encode(), True),
            (429, b"private-provider-detail", False),
            (200, b"x" * 800001, False),
            (200, b"{", False),
        ):

            async def chunks(size: int) -> AsyncIterator[bytes]:
                yield data

            response, session = MagicMock(), MagicMock()
            response.status = status
            response.__aenter__.return_value = response
            response.content.iter_chunked.side_effect = chunks
            session.__aenter__.return_value = session
            session.post.return_value = response
            with (
                self.subTest(status=status, size=len(data)),
                patch.dict(
                    os.environ,
                    {
                        "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                        "SCW_GENERATIVE_API_KEY": "test-only",
                    },
                ),
                patch("vision.aiohttp.ClientSession", return_value=session),
            ):
                if succeeds:
                    result = await VisionProvider().post({}, 2)
                    self.assertEqual(result, recorded)
                    self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
                else:
                    with self.assertRaises(RuntimeError) as error:
                        await VisionProvider().post({}, 2)
                    self.assertNotIn("private-provider-detail", str(error.exception))

    async def test_configuration_and_connection_error(self) -> None:
        with patch.dict(
            os.environ, {"SCW_GENERATIVE_BASE_URL": "http://example.invalid"}
        ):
            with self.assertRaises(ValueError):
                await VisionProvider().post({}, 2)
        with (
            patch.dict(
                os.environ, {"SCW_GENERATIVE_BASE_URL": "https://example.invalid"}
            ),
            patch(
                "vision.aiohttp.ClientSession",
                side_effect=aiohttp.ClientConnectionError("private"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "^provider_error$"):
                await VisionProvider().post({}, 2)
