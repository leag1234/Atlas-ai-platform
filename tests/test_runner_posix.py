"""REQ-RUN-07: macOS probe and safe portable process reconciliation."""

import json
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from services.runner import remote_process
from services.runner.contract import Target
from services.runner.policy import RunnerError
from services.runner.ssh import SSH, fingerprint


class PosixRunnerTests(unittest.TestCase):
    def test_default_port_pin_uses_openssh_hostname_format(self) -> None:
        public = "ssh-ed25519 " + base64.b64encode(b"x" * 32).decode()
        with tempfile.TemporaryDirectory(dir="BRAIN") as directory:
            ssh = object.__new__(SSH)
            ssh.root, ssh.jump = Path(directory), None
            for port, host in [(22, "runner.example"), (2300, "[runner.example]:2300")]:
                ssh.target = dict(
                    host="runner.example",
                    port=port,
                    host_key_fingerprints=[fingerprint(public)],
                )
                # Old checkpoints must be repaired, not reused with invalid pins.
                (ssh.root / "known_hosts").write_text(
                    "[runner.example]:22 " + public + "\n"
                )
                with patch(
                    "services.runner.ssh.process",
                    return_value=(host + " " + public + "\n").encode(),
                ):
                    self.assertEqual(ssh.pin().read_text(), host + " " + public + "\n")

    def test_target_schema_declares_macos(self) -> None:
        self.assertEqual(
            Target.model_json_schema()["properties"]["os"]["enum"], ["linux", "macos"]
        )

    def test_probe_checks_registered_os(self) -> None:
        for configured, observed in [("macos", "darwin"), ("linux", "linux")]:
            ssh = object.__new__(SSH)
            ssh.target = dict(os=configured, work_root="/work")
            value = json.dumps(
                dict(os=observed, arch="arm64", free_bytes=123, git="git version 2")
            ).encode()
            with patch.object(ssh, "run", return_value=value) as run:
                self.assertEqual(ssh.probe()["os"], configured)
                self.assertEqual(run.call_count, 1)
            with patch.object(ssh, "run", return_value=b'{"os":"windows"}'):
                with self.assertRaisesRegex(RunnerError, "unsupported_target_os"):
                    ssh.probe()

    def test_linux_identity_matches_running_process(self) -> None:
        identity = remote_process.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(identity, remote_process.process_identity(os.getpid()))
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertIsNone(remote_process.process_identity(os.getpid()))

    def test_macos_identity_and_vanished_process(self) -> None:
        reply = subprocess.CompletedProcess([], 0, "Mon Oct 26 12:00:00 2026\n", "")
        with patch.object(sys, "platform", "darwin"):
            with patch.object(subprocess, "run", return_value=reply) as run:
                self.assertEqual(
                    remote_process.process_identity(123), "Mon Oct 26 12:00:00 2026"
                )
                self.assertEqual(
                    run.call_args.args[0], ["/bin/ps", "-p", "123", "-o", "lstart="]
                )
                self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
            with patch.object(
                subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", ""),
            ):
                self.assertIsNone(remote_process.process_identity(123))

    def test_cancel_never_signals_missing_or_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory(dir="BRAIN") as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            state_dir = root.parent / "control" / "command"
            state_dir.mkdir(parents=True)
            state = dict(
                state="running",
                pid=123,
                process_start="original",
                started_at=0,
                timeout_s=1,
            )
            for identity in [None, "reused"]:
                remote_process.save(state_dir / "state.json", state)
                with (
                    patch.object(
                        remote_process, "process_identity", return_value=identity
                    ),
                    patch.object(os, "killpg") as kill,
                ):
                    result = remote_process.main(
                        dict(root=str(root), operation="cancel", command_id="command")
                    )
                    self.assertEqual(result["state"], "unknown")
                    kill.assert_not_called()
