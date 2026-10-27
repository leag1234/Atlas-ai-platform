"""Regression shapes from recorded inline checks and standard test invocations."""

import unittest

from services.orchestrator.code_verification import Verification


class CommandRecognitionTests(unittest.TestCase):
    def test_standard_and_inline_checks(self) -> None:
        for command in (
            "python -m unittest",
            "python3 -m pytest -q",
            "pytest tests/test_example.py::Example::test_value",
            "cd source && python -m unittest",
            "bash run_tests.sh",
            "./run_tests.sh",
            "make test",
            "python3 -c 'value = 2; assert value == 2'",
            "\ncd source && python3 -c 'value = 2; assert value == 2'\n",
            "cd source && python3 - <<'PY'\nvalue = 2\nassert value == 2\nPY",
            "python3 <<EOF\nvalue = 2\nassert value == 2\nEOF\n",
        ):
            with self.subTest(command=command):
                tracker = Verification()
                tracker.observe(
                    "run_command",
                    {"command": command},
                    {"exit_code": 0, "workspace_changed": False},
                )
                self.assertEqual(tracker.status().status, "passed")
                self.assertEqual(tracker.status().command, command)

    def test_success_words_are_not_checks(self) -> None:
        for command in (
            'echo "OK"',
            'echo "tests passed"',
            'printf "assert value == 2"',
            "python3 -c 'print(\"tests passed\")'",
            'python3 -c \'text = "assert False"; print("OK")\'',
            "python3 -c 'if False: assert False'",
            "python3 -c 'def test_unused(): assert False'",
            "python3 -O -c 'assert False'",
            "PYTHONOPTIMIZE=1 python3 -c 'assert False'",
            "python3 -c 'import sys; sys.exit(0); assert False'",
            "python3 -c 'try:\n assert False\nexcept AssertionError:\n pass'",
            "python3 - <<'PY'\nprint('OK')\nPY",
            "python3 - <<'PY'\nassert False\nPY\necho OK",
            "python3 - <<'PY'\nassert False\nPY\ntrue",
            "make -n test",
            "bash -n run_tests.sh",
            "echo test && python3 -c 'assert False' || true",
        ):
            with self.subTest(command=command):
                tracker = Verification()
                tracker.observe(
                    "run_command",
                    {"command": command},
                    {"exit_code": 0, "workspace_changed": False, "output": "OK"},
                )
                self.assertNotEqual(tracker.status().status, "passed")
