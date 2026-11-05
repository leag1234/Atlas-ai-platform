import json


class TransientError(Exception):
    """A failure worth retrying (connection reset, timeout, gateway error)."""


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class Client:
    """``transport(url)`` returns ``(status, body_bytes)`` or raises TransientError."""

    def __init__(self, transport):
        self.transport = transport

    def get_json(self, url):
        status, body = self.transport(url)
        if status != 200:
            raise HTTPError(status)
        return json.loads(body)
