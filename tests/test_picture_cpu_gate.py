"""The CPU picture server's queue (infra/pictures-cpu/gate.py): first come,
first served jobs, admitted only when they fit the caller's wait."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

GATE = Path(__file__).resolve().parents[1] / "infra/pictures-cpu/gate.py"


def _gate() -> Any:
    spec = importlib.util.spec_from_file_location("pictures_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pictures_gate"] = module  # dataclasses resolve their module
    spec.loader.exec_module(module)
    return module


gate = _gate()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.queue = gate.Queue(per_image=20.0, max_images=8, clock=self.clock)

    def test_admission_follows_the_expected_wait(self) -> None:
        first, expected = self.queue.admit(2, 100.0)
        self.assertIsNotNone(first)
        self.assertEqual(expected, 40.0)
        second, expected = self.queue.admit(2, 100.0)
        self.assertIsNotNone(second)
        self.assertEqual(expected, 80.0)
        third, expected = self.queue.admit(2, 100.0)
        self.assertIsNone(third)  # 120 s would not fit 100 s
        self.assertEqual(expected, 120.0)
        self.assertEqual(self.queue.images(), 4)
        # A caller with more time is still admitted.
        fourth, _ = self.queue.admit(2, 130.0)
        self.assertIsNotNone(fourth)

    def test_queue_length_limit_holds_without_a_wait(self) -> None:
        for _ in range(4):
            job, _ = self.queue.admit(2, None)
            self.assertIsNotNone(job)
        job, _ = self.queue.admit(1, None)
        self.assertIsNone(job)

    def test_busy_seconds_count_the_picture_being_drawn(self) -> None:
        job, _ = self.queue.admit(2, None)
        self.assertTrue(self.queue.wait_turn(job))
        self.queue.start_image()
        self.clock.now += 15.0
        self.assertEqual(self.queue.busy_seconds(), 25.0)
        self.queue.image_done(job, 24.0)
        self.assertEqual(self.queue.per_image, 20.8)  # 0.8 * 20 + 0.2 * 24
        self.assertAlmostEqual(self.queue.busy_seconds(), 20.8)
        self.queue.finish(job)
        self.assertEqual(self.queue.busy_seconds(), 0.0)

    def test_a_job_whose_caller_left_is_dropped(self) -> None:
        first, _ = self.queue.admit(1, None)
        late, _ = self.queue.admit(1, 50.0)
        self.assertIsNotNone(late)
        self.assertTrue(self.queue.wait_turn(first))
        self.clock.now += 50.0 + gate.GRACE_SECONDS + 1
        self.queue.finish(first)
        self.assertFalse(self.queue.wait_turn(late))
        self.assertEqual(self.queue.images(), 0)

    def test_jobs_are_served_first_come_first_served(self) -> None:
        queue = gate.Queue(per_image=1.0, max_images=20, clock=time.monotonic)
        order: list[int] = []
        jobs = [queue.admit(1, None)[0] for _ in range(5)]

        def work(index: int) -> None:
            job = jobs[index]
            assert queue.wait_turn(job)
            order.append(index)
            time.sleep(0.01)
            queue.finish(job)

        threads = [threading.Thread(target=work, args=(i,)) for i in (4, 2, 0, 3, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(order, [0, 1, 2, 3, 4])
