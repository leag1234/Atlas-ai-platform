"""A recorded M10 paragraph must select the same passage across hash seeds."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ContentDeterminismTests(unittest.TestCase):
    def test_equal_relevance_is_independent_of_hash_seed(self) -> None:
        fixture = Path("tests/fixtures/content-tie.json")
        expected = json.loads(fixture.read_text())["expected"]
        program = """import json
from services.orchestrator.content import select_passages
case=json.load(open("tests/fixtures/content-tie.json"))
print(json.dumps([p.start for p in select_passages(case["text"],case["query"],1199)]))
"""
        for seed in ("0", "9", "31"):
            with self.subTest(hash_seed=seed):
                result = subprocess.run(
                    [sys.executable, "-c", program],
                    env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH="."),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(json.loads(result.stdout), expected)
