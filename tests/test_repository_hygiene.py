"""Working notes under BRAIN/ are local agent state and must never be tracked."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_working_notes_are_tracked(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "BRAIN"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual(result.stdout.split(), [])


if __name__ == "__main__":
    unittest.main()
