"""REQ-RUN-03: Docker DNS works with the broker's restrictive firewall."""

from pathlib import Path
import subprocess
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class RunnerNetworkTests(unittest.TestCase):
    def test_resolver_loopback_and_registered_endpoint_only(self) -> None:
        def docker(*args: str) -> str:
            return subprocess.run(
                ["docker", *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            ).stdout.strip()

        docker(
            "build",
            "-q",
            "-t",
            "atlas-runner-network-test:local",
            str(ROOT / "tests/runner"),
        )
        network = "atlas-runner-dns-" + uuid4().hex[:10]
        docker("network", "create", network)
        self.addCleanup(docker, "network", "rm", network)
        peer = docker(
            "run",
            "-d",
            "--rm",
            "--network",
            network,
            "--network-alias",
            "runner-peer",
            "atlas-runner-network-test:local",
        )
        self.addCleanup(docker, "rm", "-f", peer)
        broker = docker(
            "run",
            "-d",
            "--rm",
            "--network",
            network,
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "-v",
            str(ROOT / "services") + ":/code/services:ro",
            "-e",
            "PYTHONPATH=/code",
            "-e",
            "ATLAS_RUNNER_FIREWALL=1",
            "--entrypoint",
            "sleep",
            "atlas-runner-network-test:local",
            "120",
        )
        self.addCleanup(docker, "rm", "-f", broker)
        output = docker(
            "exec",
            broker,
            "python3",
            "-c",
            """
import socket
from services.runner.network import initialize, allow
initialize()
address = socket.gethostbyname('runner-peer')
try:
    socket.create_connection((address, 2222), timeout=1)
except TimeoutError:
    print('unregistered endpoint blocked')
else:
    raise AssertionError('unregistered endpoint was reachable')
allow('runner-peer', 2222)
with socket.create_connection(('runner-peer', 2222), timeout=5) as connection:
    assert connection.recv(128).startswith(b'SSH-2.0-')
print('registered hostname reachable')
""",
        )
        self.assertIn("unregistered endpoint blocked", output)
        self.assertIn("registered hostname reachable", output)
