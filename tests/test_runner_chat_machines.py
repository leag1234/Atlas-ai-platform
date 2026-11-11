"""Machine tools of the conversation against the real member machines API.

Same fixture as test_runner_machines: real tenant database with row-level
security, the real runner broker and a disposable SSH account on loopback.
The tools reach the API as the member, the way Work does through
ExecutionBroker.member.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from services.orchestrator import work_machine_tools as machines
from services.orchestrator.work_tool_selection import WorkToolSelection
from test_runner_machines import HOSTILE_HOSTS, MachineTests


class MemberRoutes:
    """ExecutionBroker.member, served by the test client as one member."""

    def __init__(self, test: MachineTests, role: str) -> None:
        self.test, self.role = test, role

    async def member(
        self,
        method: str,
        tail: str,
        data: dict[str, Any] | None = None,
        revision: object = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, Any]]:
        reply = self.test.request(
            method,
            f"/api/atlas/orgs/{self.test.org}/{tail}",
            self.role,
            data,
            match=revision if isinstance(revision, int) else None,
        )
        return reply.status_code, reply.json()


class ChatMachineTests(MachineTests):
    def tools(self, role: str = "editor") -> Any:
        return SimpleNamespace(
            execution=SimpleNamespace(broker=MemberRoutes(self, role)),
            lang="en",
            approvals_unattended=True,
            machines_declared=set(),
            machine_commands={},
            selection=WorkToolSelection("remote"),
        )

    def use(self, tools: Any, tool: str, **arguments: Any) -> dict[str, Any]:
        with patch.dict(os.environ, {"ATLAS_REMOTE_RUNNERS": "1"}):
            return asyncio.run(machines.execute(tools, tool, json.dumps(arguments)))

    def add(self, tools: Any, **changes: Any) -> dict[str, Any]:
        values = dict(name="build", host="127.0.0.1", port=self.ssh_port, user="runner")
        values.update(changes)
        return self.use(tools, "machine_add", **values)

    def test_chat_machine_set_up_verified_and_removed_through_the_tools(
        self,
    ) -> None:
        turn = self.tools()
        added = self.add(turn)
        self.assertNotIn("error", added, added)
        listed = self.request("GET", self.machines, "editor").json()["items"]
        self.assertEqual(len(listed), 1)
        machine = listed[0]
        self.assertEqual(added["install_command"], machine["install_command"])
        self.assertEqual(machine["permission_mode"], "auto")
        for hidden in ("credential_ref", "private_key", "PRIVATE KEY", machine["id"]):
            self.assertNotIn(hidden, json.dumps(added))
        # Same turn: no connection attempt is made.
        early = self.use(turn, "machine_verify", name="build")
        self.assertEqual(early["error"], "awaiting_user")
        self.assertIsNone(
            self.request("GET", self.machines, "editor").json()["items"][0][
                "last_probe"
            ]
        )
        # Next turn, before the member ran the command.
        turn = self.tools()
        refused = self.use(turn, "machine_verify", name="build")
        self.assertEqual(refused["error"], "credential_unavailable", refused)
        self.assertEqual(refused["install_command"], machine["install_command"])
        self.assertNotIn("credential_unavailable", refused["message"])
        self.install(machine)
        verified = self.use(turn, "machine_verify", name="build")
        self.assertNotIn("error", verified, verified)
        self.assertTrue(verified["machine"]["verified"])
        self.assertIn(verified["machine"]["host_key_fingerprint"], verified["message"])
        names = [t["name"] for t in self.remote("list", {}).json()["items"]]
        self.assertIn("build", names)
        # Another member of the same organization sees nothing and changes nothing.
        other = self.tools("owner")
        self.assertEqual(self.use(other, "machine_list")["machines"], [])
        for tool, arguments in (
            ("machine_verify", {}),
            ("machine_change", dict(permission_mode="rules")),
            ("machine_forget", dict(confirmed=True)),
        ):
            with self.subTest(tool=tool):
                hidden = self.use(other, tool, name="build", **arguments)
                self.assertEqual(hidden["error"], "machine_not_found")
        # Removal needs the member's yes.
        refused = self.use(turn, "machine_forget", name="build")
        self.assertEqual(refused["error"], "confirmation_required")
        self.assertEqual(
            len(self.request("GET", self.machines, "editor").json()["items"]), 1
        )
        self.assertEqual(len(self.key_files()), 1)
        removed = self.use(turn, "machine_forget", name="build", confirmed=True)
        self.assertTrue(removed.get("removed"), removed)
        self.assertEqual(
            self.request("GET", self.machines, "editor").json()["items"], []
        )
        self.assertEqual(self.key_files(), [])

    def test_chat_machine_hostile_input_is_refused_before_any_key_exists(
        self,
    ) -> None:
        for host in HOSTILE_HOSTS:
            if not host:
                continue
            with self.subTest(host=host):
                refused = self.add(self.tools(), host=host)
                self.assertEqual(refused["error"], "invalid_machine", refused)
                self.assertNotIn(host.strip(), refused["message"])
        self.assertEqual(
            self.request("GET", self.machines, "editor").json()["items"], []
        )
        self.assertEqual(self.key_files(), [])

    def test_chat_machine_private_address_is_explained(self) -> None:
        with patch.dict(os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": ""}):
            self.add(self.tools())
            refused = self.use(self.tools(), "machine_verify", name="build")
        self.assertEqual(refused["error"], "runner_target_address_denied", refused)
        self.assertIn("private or reserved network", refused["message"])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    # The parent fixture's own tests run in their modules; run only these.
    return unittest.TestSuite(
        ChatMachineTests(name)
        for name in loader.getTestCaseNames(ChatMachineTests)
        if name.startswith("test_chat_machine_")
    )
