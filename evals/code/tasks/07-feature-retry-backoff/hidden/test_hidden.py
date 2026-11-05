import unittest

from fetcher import Client, HTTPError, TransientError
from fetcher.retry import call_with_retry


class Flaky:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class HiddenRetry(unittest.TestCase):
    def test_delays(self):
        sleeps = []
        fn = Flaky([TransientError(), TransientError(), TransientError(), "ok"])
        self.assertEqual(call_with_retry(fn, attempts=4, sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])

    def test_cap_and_last_error(self):
        sleeps = []
        errors = [TransientError(str(i)) for i in range(6)]
        fn = Flaky(errors)
        with self.assertRaises(TransientError) as ctx:
            call_with_retry(fn, attempts=6, base_delay=1, max_delay=5, sleep=sleeps.append)
        self.assertIs(ctx.exception, errors[-1])
        self.assertEqual(sleeps, [1, 2, 4, 5, 5])
        self.assertEqual(fn.calls, 6)

    def test_non_retryable(self):
        sleeps = []
        fn = Flaky([KeyError("x"), "ok"])
        with self.assertRaises(KeyError):
            call_with_retry(fn, sleep=sleeps.append)
        self.assertEqual((sleeps, fn.calls), ([], 1))

    def test_custom_retry_on(self):
        sleeps = []
        fn = Flaky([OSError(), "ok"])
        self.assertEqual(call_with_retry(fn, retry_on=(OSError,), sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [0.5])

    def test_attempts_validation(self):
        with self.assertRaises(ValueError):
            call_with_retry(lambda: 1, attempts=0, sleep=lambda s: None)
        self.assertEqual(call_with_retry(lambda: 1, attempts=1, sleep=lambda s: None), 1)

    def test_client_retries_gateway(self):
        sleeps = []
        transport = Flaky([(503, b""), TransientError(), (200, b'[1]')])
        client = Client(transport, attempts=3, sleep=sleeps.append)
        self.assertEqual(client.get_json("u"), [1])
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_client_gives_up(self):
        sleeps = []
        transport = Flaky([(502, b""), (504, b"")])
        client = Client(transport, attempts=2, sleep=sleeps.append)
        with self.assertRaises(TransientError):
            client.get_json("u")
        self.assertEqual(sleeps, [0.5])

    def test_client_404_not_retried(self):
        sleeps = []
        transport = Flaky([(404, b""), (200, b"1")])
        with self.assertRaises(HTTPError):
            Client(transport, sleep=sleeps.append).get_json("u")
        self.assertEqual((sleeps, transport.calls), ([], 1))

    def test_default_client(self):
        self.assertEqual(Client(lambda url: (200, b"2")).get_json("u"), 2)
