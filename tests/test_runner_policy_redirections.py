import re
import unittest
from pathlib import Path

from services.orchestrator.code_tools import REMOTE_FIX_HINTS
from services.runner.contract import RemoteRun
from services.runner.policy import RunnerError, commands, decision


class HarmlessRedirectionTests(unittest.TestCase):
    def test_dev_null_and_descriptor_duplication_are_accepted(self) -> None:
        accepted = {
            "tail -100 /var/log/syslog 2>/dev/null || journalctl -n 100 --no-pager 2>/dev/null": [
                ["tail", "-100", "/var/log/syslog"],
                ["journalctl", "-n", "100", "--no-pager"],
            ],
            "df -h 2>&1 | head -5": [["df", "-h"], ["head", "-5"]],
            "ls >/dev/null": [["ls"]],
            "uptime &>/dev/null; free -m": [["uptime"], ["free", "-m"]],
            "grep -r error /var/log < /dev/null": [["grep", "-r", "error", "/var/log"]],
        }
        for command, argv in accepted.items():
            with self.subTest(command=command):
                self.assertEqual(commands(command), argv)
                RemoteRun(target="box", command=command)

    def test_redirections_that_touch_files_stay_refused(self) -> None:
        for command in (
            "echo x > ~/.bashrc",
            "cat a 2>/dev/nullx",
            "ls 2>/tmp/f",
            "ls >/dev/null>/etc/passwd",
            "cat </etc/shadow",
            "ls 3>&1",
            "ls >>/dev/null2",
        ):
            with self.subTest(command=command):
                with self.assertRaises(RunnerError):
                    commands(command)

    def test_policy_rules_still_see_every_command_of_the_chain(self) -> None:
        self.assertEqual(
            decision("ls 2>/dev/null; rm -rf ~", "auto", [], ["rm *"], [], []),
            "denied",
        )

    def test_every_command_shape_error_has_a_rewrite_hint(self) -> None:
        source = Path("services/runner/policy.py").read_text()
        codes = set(
            re.findall(r'RunnerError\("([a-z_]+)"\)', source.split("def matches")[0])
        )
        self.assertEqual(codes, set(REMOTE_FIX_HINTS))
        for hint in REMOTE_FIX_HINTS.values():
            self.assertNotRegex(hint.lower(), r"no access|cannot access")


if __name__ == "__main__":
    unittest.main()
