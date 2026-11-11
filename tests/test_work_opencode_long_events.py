import json
import unittest

import aiohttp
from aiohttp import web
from aiohttp.http_exceptions import LineTooLong
from aiohttp.test_utils import TestServer

from services.orchestrator.work_opencode import EVENT_LINE_BYTES


class LongEventLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_server_sent_event_of_several_megabytes_is_read_whole(self) -> None:
        event = (
            "data: "
            + json.dumps({"type": "message.part.updated", "text": "x" * 3_000_000})
            + "\n"
        )

        async def events(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(event.encode())
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/event", events)
        async with TestServer(app) as server:
            async with aiohttp.ClientSession() as http:
                async with http.get(server.make_url("/event")) as stream:
                    line = await stream.content.readline(
                        max_line_length=EVENT_LINE_BYTES
                    )
            async with aiohttp.ClientSession() as http:
                async with http.get(server.make_url("/event")) as stream:
                    with self.assertRaises(LineTooLong):
                        await stream.content.readline()
        self.assertEqual(line.decode(), event)


if __name__ == "__main__":
    unittest.main()
