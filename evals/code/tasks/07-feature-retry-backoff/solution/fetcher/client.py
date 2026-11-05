import json
import time


class TransientError(Exception):
    """A failure worth retrying (connection reset, timeout, gateway error)."""


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class Client:
    """``transport(url)`` returns ``(status, body_bytes)`` or raises TransientError."""

    def __init__(self, transport, attempts=3, sleep=time.sleep):
        self.transport = transport
        self.attempts = attempts
        self.sleep = sleep

    def _once(self, url):
        status, body = self.transport(url)
        if status in (502, 503, 504):
            raise TransientError(f"HTTP {status}")
        if status != 200:
            raise HTTPError(status)
        return json.loads(body)

    def get_json(self, url):
        from fetcher.retry import call_with_retry

        return call_with_retry(lambda: self._once(url), attempts=self.attempts, sleep=self.sleep)
