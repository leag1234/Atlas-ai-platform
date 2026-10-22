"""M8 HTTP configuration stays model-independent and rejects ambiguous options."""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from gateway_cpu import CPUModels
from http_gateway import serve
from serverless import ServerlessPolicy
from serverless_support import environment


class ServerlessHTTPTests(unittest.TestCase):
    def test_configuration_and_validation(self) -> None:
        with patch.dict("os.environ", environment()):
            server = serve(Mock(spec=CPUModels), 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/agent/config"

                def request(payload: dict[str, object]) -> dict[str, object]:
                    with urlopen(
                        Request(url, data=json.dumps(payload).encode()), timeout=5
                    ) as response:
                        value = json.load(response)
                        assert isinstance(value, dict)
                        return value

                self.assertEqual(
                    request({"local_enabled": False}),
                    ServerlessPolicy().configuration(),
                )
                invalid: tuple[dict[str, object], ...] = (
                    {"local_enabled": "false"},
                    {"unknown": False},
                )
                for payload in invalid:
                    with self.assertRaises(HTTPError) as error:
                        request(payload)
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
