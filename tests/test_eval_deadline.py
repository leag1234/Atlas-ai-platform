"""POC-P6: a slow trickle must not bypass the wall-clock deadline."""

import time
import unittest

from eval_provider import deadline


class DeadlineTests(unittest.TestCase):
    def test_slow_operation_is_interrupted_and_timer_restored(self) -> None:
        import signal

        old = signal.getsignal(signal.SIGALRM)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            with deadline(0.02):
                time.sleep(1)
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertEqual(signal.getsignal(signal.SIGALRM), old)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
