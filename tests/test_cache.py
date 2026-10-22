"""POC-W1: persistent expiry and atomic monthly quota."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from services.orchestrator.cache import Cache


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Cache(Path(directory.name) / "cache.sqlite")

    def test_expiry_and_atomic_quota(self) -> None:
        self.cache.put("key", {"x": "é"}, 3600)
        self.assertEqual(self.cache.get("key"), {"x": "é"})
        with patch("time.time", return_value=10**12):
            self.assertIsNone(self.cache.get("key"))
        for _ in range(898):
            self.cache.reserve_search()

        def reserve(_: int) -> bool:
            try:
                self.cache.reserve_search()
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 2)
