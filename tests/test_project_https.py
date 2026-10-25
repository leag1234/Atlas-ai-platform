"""REQ-PROJ-07/08: public HTTPS origin through an internal HTTP proxy hop."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from starlette.requests import Request

from services.orchestrator import chat_api


class ProjectHttpsTests(unittest.TestCase):
    def request(self, origin: str | None, site: str | None = "same-origin") -> Request:
        headers = [(b"host", b"test-atlas.indidea.org:10443")]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        if site is not None:
            headers.append((b"sec-fetch-site", site.encode()))

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"{", "more_body": False}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "scheme": "http",
                "server": ("orchestrator", 13720),
                "query_string": b"",
                "headers": headers,
            },
            receive,
        )

    def test_https_origin_reaches_body_validation_without_trusting_forwarded_headers(
        self,
    ) -> None:
        origin = "https://test-atlas.indidea.org:10443"
        with (
            patch.dict(
                os.environ, {"ATLAS_PROJECT_MODE": "1", "ATLAS_PROJECT_ORIGIN": origin}
            ),
            patch.object(chat_api, "write_interaction"),
        ):
            response = asyncio.run(chat_api.chat(self.request(origin)))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                json.loads(bytes(response.body))["error"]["code"], "invalid_request"
            )
            for rejected, site in [
                (None, "same-origin"),
                ("https://elsewhere.invalid", "same-origin"),
                (origin, "cross-site"),
            ]:
                response = asyncio.run(chat_api.chat(self.request(rejected, site)))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    json.loads(bytes(response.body))["error"], "origin_rejected"
                )


if __name__ == "__main__":
    unittest.main()
