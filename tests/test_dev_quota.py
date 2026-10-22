"""REQ-DEV-002: quota reservation is atomic, durable and conservative on failure."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.orchestrator.dev_auth import Store, provision, QuotaError


class DevQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        clock = patch(
            "services.orchestrator.dev_auth.utc_day", return_value="2030-01-01"
        )
        clock.start()
        self.addCleanup(clock.stop)

    def test_parallel_reservations_crash_restart_and_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "usage.sqlite")
            provision(store, "alice", root / "alice", 10, 50000)
            provision(store, "bob", root / "bob", 1, 50000)

            def reserve(index: int) -> str | None:
                try:
                    return Store(store.path).reserve("alice", 25000)
                except QuotaError:
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                accepted = [r for r in pool.map(reserve, range(8)) if r is not None]
            self.assertEqual(len(accepted), 2)
            restarted = Store(store.path)
            usage = restarted.usage("alice")
            self.assertEqual(usage["requests"], 2)
            self.assertEqual(usage["charged_micro_eur"], 50000)
            self.assertEqual(usage["unknown_micro_eur"], 50000)
            restarted.finish(accepted[0], 1000, 100, 20)
            restarted.finish(accepted[1], None)
            self.assertEqual(restarted.usage("alice")["charged_micro_eur"], 26000)
            self.assertEqual(restarted.usage("alice")["unknown_micro_eur"], 25000)
            with self.assertRaises(ValueError):
                restarted.finish(accepted[1], 0, 0, 0)
            self.assertEqual(restarted.usage("bob")["requests"], 0)
            restarted.reserve("alice", 24000)
            with self.assertRaises(QuotaError):
                restarted.reserve("alice", 1)
            restarted.reserve("bob", 1)
            with self.assertRaises(QuotaError):
                restarted.reserve("bob", 1)
            with patch(
                "services.orchestrator.dev_auth.utc_day", return_value="2099-01-01"
            ):
                self.assertEqual(restarted.usage("alice")["requests"], 0)
                restarted.reserve("alice", 50000)
            restarted.revoke("bob")
            with self.assertRaises(PermissionError):
                restarted.reserve("bob", 1)
            for invalid in (-1, 0, 50001, True):
                with self.subTest(amount=invalid), self.assertRaises(ValueError):
                    restarted.reserve("alice", invalid)

    def test_invalid_settlement_never_releases_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "usage.sqlite")
            provision(store, "alice", root / "alice", 5, 50000)
            request_id = store.reserve("alice", 10000)
            for charge, incoming, outgoing in ((-1, 1, 1), (1, -1, 1), (1, 1, -1)):
                with self.assertRaises(ValueError):
                    store.finish(request_id, charge, incoming, outgoing)
                self.assertEqual(store.usage("alice")["charged_micro_eur"], 10000)
            store.finish(request_id, None)
            self.assertEqual(store.usage("alice")["unknown_micro_eur"], 10000)
