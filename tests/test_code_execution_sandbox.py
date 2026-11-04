"""Real-container security checks for the API code execution sandbox."""

import asyncio
import shutil
import subprocess
import unittest

from services.orchestrator.terminal_client import (
    code_execution_arguments,
    code_execution_sandbox,
)
from services.orchestrator.terminal_stack import IMAGE


class SandboxArgumentTests(unittest.TestCase):
    def test_flags_are_fixed_and_nothing_is_mounted(self) -> None:
        arguments = code_execution_arguments("atlas-exec-x", "image")
        joined = " ".join(arguments)
        for flag in (
            "--rm",
            "--network none",
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            "--read-only",
            "--user 1000:1000",
            "--memory 1g",
            "--memory-swap 1g",
            "--cpus 1",
            "--pids-limit 64",
            "--pull never",
            "--label atlas.owner=exec",
        ):
            self.assertIn(flag, joined)
        self.assertIn("size=256m", joined)
        for forbidden in ("-v", "--volume", "--mount", "--privileged", "--env-file"):
            self.assertNotIn(forbidden, arguments)
        self.assertEqual(arguments[-2:], ["image", "-"])

    def test_oversized_code_is_refused_without_docker(self) -> None:
        result = asyncio.run(code_execution_sandbox("x" * (256 * 1024 + 1)))
        self.assertEqual(result, {"error": "invalid_tool_input"})


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)


@unittest.skipUnless(
    shutil.which("docker") is not None
    and docker("image", "inspect", IMAGE).returncode == 0,
    "Docker and the terminal image are required",
)
class CodeExecutionSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(self.assert_no_exec_containers)

    def assert_no_exec_containers(self) -> None:
        listing = docker("ps", "-aq", "--filter", "label=atlas.owner=exec")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(listing.stdout, "")

    def run_code(self, code: str, timeout: float = 60.0) -> dict[str, object]:
        return asyncio.run(code_execution_sandbox(code, timeout))

    def test_network_is_disabled(self) -> None:
        result = self.run_code(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('example.com', 80), 2)\n"
            "except OSError:\n"
            "    raise SystemExit(3)\n"
            "raise SystemExit(9)\n"
        )
        self.assertEqual(result["return_code"], 3)

    def test_no_host_or_cross_request_files_persist(self) -> None:
        first = self.run_code(
            "from pathlib import Path\nPath('/tmp/hostile').write_text('secret')\n"
        )
        second = self.run_code(
            "from pathlib import Path\n"
            "raise SystemExit(3 if Path('/tmp/hostile').exists() else 4)\n"
        )
        self.assertEqual(first["return_code"], 0)
        self.assertEqual(second["return_code"], 4)

    def test_host_files_and_environment_are_not_visible(self) -> None:
        result = self.run_code(
            "import os\n"
            "from pathlib import Path\n"
            "assert not Path('/opt/atlas-src').exists()\n"
            "assert not any(k.startswith(('ATLAS', 'SCW', 'TAVILY', 'SERPAPI')) for k in os.environ)\n"
            "assert sorted(os.listdir('/tmp')) == []\n"
            "print('clean')\n"
        )
        self.assertEqual(result["return_code"], 0, result)
        self.assertEqual(result["stdout"], "clean\n")

    def test_cancellation_removes_container(self) -> None:
        async def cancelled() -> None:
            task = asyncio.ensure_future(
                code_execution_sandbox("import time\ntime.sleep(30)\n", 60.0)
            )
            await asyncio.sleep(2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancelled())

    def test_writes_outside_tmp_fail(self) -> None:
        result = self.run_code(
            "from pathlib import Path\n"
            "try:\n"
            "    Path('/home/user/hostile').write_text('x')\n"
            "except OSError:\n"
            "    raise SystemExit(3)\n"
            "raise SystemExit(9)\n"
        )
        self.assertEqual(result["return_code"], 3)

    def test_timeout_kills_infinite_loop_and_removes_container(self) -> None:
        result = self.run_code("while True:\n    pass\n", timeout=1.0)
        self.assertEqual(result, {"error": "execution_time_exceeded"})

    def test_memory_limit_fails_cleanly(self) -> None:
        result = self.run_code("x = bytearray(1024 * 1024 * 1024)\n")
        self.assertNotEqual(result["return_code"], 0)
        self.assertIn("return_code", result)

    def test_pid_limit_stops_fork_bomb(self) -> None:
        result = self.run_code(
            "import os\n"
            "while True:\n"
            "    try:\n"
            "        os.fork()\n"
            "    except OSError:\n"
            "        break\n"
        )
        self.assertIn("return_code", result)

    def test_stdout_and_stderr_are_capped_independently(self) -> None:
        result = self.run_code(
            "import sys\n"
            "sys.stdout.write('o' * (64 * 1024 + 1024))\n"
            "sys.stderr.write('e' * (64 * 1024 + 1024))\n"
        )
        self.assertEqual(len(str(result["stdout"])), 64 * 1024)
        self.assertEqual(len(str(result["stderr"])), 64 * 1024)
        self.assertEqual(result["stdout"], "o" * 64 * 1024)
        self.assertEqual(result["stderr"], "e" * 64 * 1024)


if __name__ == "__main__":
    unittest.main()
