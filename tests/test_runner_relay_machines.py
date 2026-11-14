"""Connected computers against the real member machines API and runner broker.

Same fixture as test_runner_machines (tenant database with row-level
security, the broker over its signed channel). No relay runs here: the gate
file is written in a temporary directory and announcements are signed with a
key generated for the test. tests/relay_live.py runs the whole path.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import patch

from services.runner import relay
from test_runner_machines import MachineTests

RELAY_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFictionalRelayKeyForTestsOnly00000000000000"
)


class ConnectedComputerTests(MachineTests):
    def setUp(self) -> None:
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_RELAY_GATE": folder.name + "/gate/hosts",
                "ATLAS_RELAY_HOST_KEY": RELAY_KEY,
                "ATLAS_RELAY_SSH": "127.0.0.1:9",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                folder.name + "/host",
            ],
            check=True,
            capture_output=True,
        )
        self.host_key = Path(folder.name, "host.pub").read_text().strip()

    def connect(self, role: str = "editor") -> dict[str, Any]:
        reply = self.request(
            "POST", self.machines, role, dict(name="ada-laptop", transport="relay")
        )
        self.assertEqual(reply.status_code, 201, reply.text)
        return dict(reply.json())

    def enrol(self, machine: dict[str, Any], nonce: str, role: str = "editor") -> Any:
        return self.request(
            "POST",
            self.machines + "/" + machine["id"] + "/enrol",
            role,
            dict(
                nonce=nonce,
                host_key=self.host_key,
                user="ada",
                os="linux",
                arch="x86_64",
                work_root="/home/ada/orbit",
            ),
        )

    def announce(
        self, machine: dict[str, Any], user: str | None, at: int, key: str = "host"
    ) -> Any:
        text = relay.message(machine["id"], user, at)
        signature = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(self.folder / key),
                "-n",
                "atlas-connect",
            ],
            input=text,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return self.request(
            "POST",
            self.machines + "/" + machine["id"] + "/announce",
            "editor",
            dict(message=text, signature=signature),
        )

    def test_one_time_enrolment_gate_and_signed_sessions(self) -> None:
        machine = self.connect()
        nonce = machine.pop("enrol_nonce")
        self.assertNotIn("install_command", machine)
        self.assertEqual(machine["transport"], "relay")
        self.assertFalse(machine["connected"])
        listed = self.request("GET", self.machines, "editor").json()["items"]
        self.assertNotIn(nonce, str(listed))
        # Another member cannot use the token's machine, nor a wrong nonce.
        self.assertEqual(self.enrol(machine, nonce, role="viewer").status_code, 404)
        self.assertEqual(self.enrol(machine, "x" * 43).status_code, 410)
        enrolled = self.enrol(machine, nonce)
        self.assertEqual(enrolled.status_code, 200, enrolled.text)
        self.assertEqual(enrolled.json()["guest_key"], machine["public_key"])
        self.assertEqual(enrolled.json()["relay_host_key"], RELAY_KEY)
        gate = (self.folder / "gate/hosts").read_text()
        self.assertIn(
            " ".join(self.host_key.split()[:2]) + " atlas-" + machine["id"], gate
        )
        # The token works once.
        self.assertEqual(self.enrol(machine, nonce).status_code, 410)
        now = int(time.time())
        # A session announced with another key is refused.
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.folder / "other"),
            ],
            check=True,
            capture_output=True,
        )
        refused = self.announce(
            machine, "dfQO2yU7r1nqhbS4LCva:Wzo6XToyMjIy", now, "other"
        )
        self.assertNotEqual(refused.status_code, 200, refused.text)
        started = self.announce(machine, "dfQO2yU7r1nqhbS4LCva:Wzo6XToyMjIy", now)
        self.assertEqual(started.status_code, 200, started.text)
        self.assertTrue(started.json()["connected"])
        current = self.request(
            "GET", self.machines + "/" + machine["id"], "editor"
        ).json()
        self.assertTrue(current["connected"])
        # No relay listens in this test: the check through it fails and says so.
        self.assertEqual(current["status"], "unverified")
        replay = self.announce(machine, "dfQO2yU7r1nqhbS4LCva:Wzo6XToyMjIy", now)
        self.assertEqual(replay.status_code, 409, replay.text)
        ended = self.announce(machine, None, now + 1)
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertFalse(ended.json()["connected"])
        # Forgetting the computer removes it from the relay gate.
        current = self.request(
            "GET", self.machines + "/" + machine["id"], "editor"
        ).json()
        deleted = self.request(
            "DELETE",
            self.machines + "/" + machine["id"],
            "editor",
            match=current["revision"],
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertNotIn(
            "atlas-" + machine["id"], (self.folder / "gate/hosts").read_text()
        )

    def test_connecting_again_renews_the_token_and_keeps_the_key(self) -> None:
        first = self.connect()
        again = self.connect()
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["public_key"], again["public_key"])
        self.assertNotEqual(first["enrol_nonce"], again["enrol_nonce"])
        self.assertEqual(self.enrol(first, first["enrol_nonce"]).status_code, 410)
        self.assertEqual(self.enrol(again, again["enrol_nonce"]).status_code, 200)
        clash = self.declare(name="ada-laptop")
        self.assertEqual(clash.status_code, 409, clash.text)


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    return unittest.TestSuite(
        ConnectedComputerTests(name)
        for name in ConnectedComputerTests.__dict__
        if name.startswith("test_")
    )
