"""REQ-RUN-03/04: real disposable SSH, no provider or cloud credentials."""

from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
import unittest
from uuid import uuid4

from services.orchestrator.project_forge import canonical
from services.runner.broker import Broker
from services.runner.contract import Target
from services.runner.policy import RunnerError, decision
from services.runner.ssh import Keys, fingerprint

ROOT = Path(__file__).resolve().parents[1]


class RunnerPolicyTests(unittest.TestCase):
    def test_every_pipeline_and_chain_part_is_checked(self) -> None:
        def decide(command: str) -> str:
            return decision(command, "rules", ["make *", "cat *"], ["sudo *"], [], [])

        self.assertEqual(decide("make test && cat 'test log'"), "auto_rule")
        self.assertEqual(decide("make test | cat"), "auto_rule")
        self.assertEqual(decide("make test && sudo reboot"), "denied")
        self.assertEqual(
            decide("make test && python -c 'print(1)'"), "awaiting_approval"
        )
        self.assertEqual(decide("make-evil test"), "awaiting_approval")
        for value in [
            "make $(sudo reboot)",
            "make `id`",
            "make test &",
            "make test > /etc/x",
            "env make test",
        ]:
            with self.subTest(value=value), self.assertRaises(RunnerError):
                decide(value)
        self.assertEqual(
            decision("sudo reboot", "auto", [], ["sudo *"], [], []), "denied"
        )
        self.assertEqual(
            decision("make clean", "auto", [], [], ["make clean"], []),
            "awaiting_approval",
        )
        self.assertEqual(
            decision("make test", "ask", ["make *"], [], [], []), "awaiting_approval"
        )
        self.assertEqual(
            decision("make test", "ask", [], [], [], ["make test *"]), "auto_rule"
        )


class RunnerBrokerTests(unittest.TestCase):
    container: str
    port: int
    host_public: str

    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(
            ["docker", "build", "-q", "-t", "atlas-runner-test:local", "tests/runner"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=180,
        )
        cls.container = "atlas-runner-tests-" + uuid4().hex[:10]
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                cls.container,
                "-p",
                "127.0.0.1::2222",
                "atlas-runner-test:local",
            ],
            check=True,
            capture_output=True,
        )
        cls.addClassCleanup(
            subprocess.run,
            ["docker", "rm", "-f", cls.container],
            check=True,
            capture_output=True,
        )
        reply = subprocess.run(
            ["docker", "port", cls.container, "2222"],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.port = int(reply.stdout.strip().rsplit(":", 1)[1])
        cls.host_public = subprocess.run(
            [
                "docker",
                "exec",
                cls.container,
                "cat",
                "/etc/ssh/ssh_host_ed25519_key.pub",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "BRAIN")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.keys = Keys(root / "keys")
        self.org = str(uuid4())
        generated = self.keys.generate(self.org)
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container,
                "sh",
                "-c",
                "cat >> /home/runner/.ssh/authorized_keys; chown -R runner:runner /home/runner/.ssh; chmod 700 /home/runner/.ssh; chmod 600 /home/runner/.ssh/authorized_keys",
            ],
            input=(generated["public_key"] + "\n").encode(),
            check=True,
            capture_output=True,
        )
        target = Target(
            name="Disposable Linux",
            host="127.0.0.1",
            port=self.port,
            user="runner",
            work_root="/home/runner/work",
            credential_ref=generated["credential_ref"],
            credential_fingerprint=generated["fingerprint"],
            host_key_fingerprints=[fingerprint(self.host_public)],
            permission_mode="auto",
        ).model_dump()
        target["id"] = str(uuid4())
        self.auth: dict[str, Any] = dict(
            org_id=self.org,
            project_id=str(uuid4()),
            target=target,
            session=dict(id=str(uuid4())),
            grant=dict(code_egress_approved_at="approved", allow_rules=[]),
        )
        self.broker = Broker(root / "state", self.keys)
        self.content = canonical(
            dict(
                files=[
                    dict(
                        path="sample.txt",
                        mode="100644",
                        content=base64.b64encode(b"first").decode(),
                    )
                ]
            )
        )

    def command(self, text: str, seconds: int = 10) -> dict[str, Any]:
        self.auth["command"] = dict(
            id=str(uuid4()),
            command=text,
            code_digest=hashlib.sha256(self.content).hexdigest(),
            cwd=".",
            timeout_s=seconds,
            decision="auto_mode",
        )
        return self.broker.run(self.auth, self.content)

    def settle(self) -> dict[str, Any]:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.broker.status(self.auth)
            if state["state"] not in {"running", "unknown"}:
                return state
            time.sleep(0.1)
        self.fail("SSH command did not settle")

    def test_sync_incremental_cache_logs_fetch_and_cleanup(self) -> None:
        self.command(
            'python3 -c \'from pathlib import Path; print(Path("sample.txt").read_text()); Path("cache").write_text("keep")\''
        )
        result = self.settle()
        self.assertEqual(result["state"], "succeeded")
        self.assertIn("first", result["output"])
        self.content = canonical(
            dict(
                files=[
                    dict(
                        path="sample.txt",
                        mode="100644",
                        content=base64.b64encode(b"second").decode(),
                    )
                ]
            )
        )
        self.command("cat sample.txt && cat cache")
        result = self.settle()
        self.assertEqual(result["state"], "succeeded")
        self.assertIn("secondkeep", result["output"])
        full = self.broker.status(self.auth, log=True)
        self.assertIn(b"secondkeep", base64.b64decode(full["content"]))
        fetched = self.broker.fetch(self.auth, "cache")["files"]
        self.assertEqual(base64.b64decode(fetched[0]["content"]), b"keep")
        self.assertEqual(fetched[0]["sha256"], hashlib.sha256(b"keep").hexdigest())
        self.broker.close(self.auth)
        _, remote = self.broker.paths(self.auth)
        self.assertEqual(
            self.broker.transport(self.auth).run("test ! -e " + remote), b""
        )

    def test_timeout_cancel_and_reconnect_never_rerun(self) -> None:
        self.command("sleep 20", 1)
        self.assertEqual(self.settle()["state"], "timed_out")
        self.command("sleep 20")
        self.broker.status(self.auth, cancel=True)
        self.assertEqual(self.settle()["state"], "cancelled")
        self.command(
            'python3 -c \'import time; from pathlib import Path; p=Path("once"); p.write_text(p.read_text()+"x" if p.exists() else "x"); time.sleep(1)\''
        )
        restarted = Broker(self.broker.root, self.keys)
        self.assertIn(
            restarted.run(self.auth, self.content)["state"], {"running", "succeeded"}
        )
        self.assertEqual(self.settle()["state"], "succeeded")
        self.assertEqual(
            base64.b64decode(restarted.fetch(self.auth, "once")["files"][0]["content"]),
            b"x",
        )
        self.broker.close(self.auth)

    def test_fingerprint_mismatch_fails_closed_and_paths_cannot_escape(self) -> None:
        bad = copy.deepcopy(self.auth)
        bad["target"]["host_key_fingerprints"] = ["SHA256:" + "A" * 43]
        with self.assertRaisesRegex(RunnerError, "host_key_fingerprint_mismatch"):
            self.broker.transport(bad).probe()
        self.command(
            'python3 -c \'from pathlib import Path; Path("escape").symlink_to("/etc/passwd")\''
        )
        self.assertEqual(self.settle()["state"], "succeeded")
        for pattern in ["../control/*", "/etc/passwd", "escape"]:
            with self.subTest(pattern=pattern), self.assertRaises(RunnerError):
                self.broker.fetch(self.auth, pattern)
        self.broker.close(self.auth)

    def test_connection_loss_reconciles_without_duplicate_effect(self) -> None:
        self.command(
            'python3 -c \'import time; from pathlib import Path; p=Path("counter"); p.write_text(p.read_text()+"x" if p.exists() else "x"); time.sleep(1)\'',
            30,
        )
        subprocess.run(
            ["docker", "pause", self.container], check=True, capture_output=True
        )
        try:
            with self.assertRaises(RunnerError):
                self.broker.status(self.auth)
        finally:
            subprocess.run(
                ["docker", "unpause", self.container], check=True, capture_output=True
            )
        self.assertEqual(self.settle()["state"], "succeeded")
        self.broker.run(self.auth, self.content)
        fetched = self.broker.fetch(self.auth, "counter")["files"]
        self.assertEqual(base64.b64decode(fetched[0]["content"]), b"x")
        self.broker.close(self.auth)

    def test_full_large_log_report_redaction_and_idle_sweep(self) -> None:
        self.command(
            'python3 -c \'from pathlib import Path; print("x" * 100000); print("password=redaction-probe"); Path("junit.xml").write_text(\'"\'"\'<testsuite><testcase name="one"/></testsuite>\'"\'"\')\''
        )
        self.assertEqual(self.settle()["state"], "succeeded")
        full = self.broker.status(self.auth, log=True)
        content = base64.b64decode(full["content"])
        self.assertGreater(len(content), 100000)
        self.assertNotIn(b"redaction-probe", content)
        self.assertEqual(full["reports"][0]["tests"], 1)
        local, remote = self.broker.paths(self.auth)
        ssh = self.broker.transport(self.auth)
        self.broker.sweep(time.time() + 86401)
        self.assertFalse(local.exists())
        self.assertEqual(ssh.run("test ! -e " + remote), b"")


if __name__ == "__main__":
    unittest.main()
