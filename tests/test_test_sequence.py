"""The CI entry point executes units once and propagates their failure."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class TestSequenceTests(unittest.TestCase):
    def test_complete_units_run_once_in_ci_and_local_modes(self) -> None:
        for ci in ("true", "false"):
            with self.subTest(ci=ci):
                status, events = self.execute(ci, False)
                self.assertEqual(status, 0)
                self.assertEqual(events.count("units"), 1)
                self.assertEqual("regressions" in events, ci == "true")
                if ci == "true":
                    self.assertLess(events.index("units"), events.index("regressions"))

    def test_failed_units_prevent_all_later_ci_gates(self) -> None:
        status, events = self.execute("true", True)
        self.assertNotEqual(status, 0)
        self.assertEqual(events, ["verify-m0", "units"])

    def execute(self, ci: str, fail: bool) -> tuple[int, list[str]]:
        script = Path("scripts/test.sh").resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = {
                "python3": """#!/bin/bash
if [[ "$1" == -m && "$2" == unittest ]]; then
  echo units >> "$TRACE"
  [[ "$FAIL_UNITS" != 1 ]]
elif [[ "$1" == tests/ci_regressions.py ]]; then
  echo regressions >> "$TRACE"
else
  echo "$*" >> "$TRACE"
fi
""",
                "make": """#!/bin/bash
echo "$*" >> "$TRACE"
if [[ "$1" == verify-m0 ]]; then
  ATLAS_VERIFY_M0=1 bash "$TEST_SCRIPT"
fi
""",
            }
            for name, content in commands.items():
                executable = root / name
                executable.write_text(content)
                executable.chmod(0o700)
            trace = root / "trace"
            environment = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "GITHUB_ACTIONS": ci,
                "ATLAS_VERIFY_M0": "0",
                "TEST_SCRIPT": str(script),
                "TRACE": str(trace),
                "FAIL_UNITS": "1" if fail else "0",
            }
            result = subprocess.run(
                ["bash", str(script)], cwd=root, env=environment, capture_output=True
            )
            return result.returncode, trace.read_text().splitlines()
