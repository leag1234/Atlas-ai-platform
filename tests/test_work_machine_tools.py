"""Machines set up in the conversation: Work tools acting as the member.

The member machines API is replaced by an in-memory double that applies the
real input validation (services.runner.machines.Machine) and the real install
command; the real backend, broker and SSH account are exercised in
tests/test_runner_chat_machines.py. Keys here are fictional.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError

from services.orchestrator import work_machine_tools as machines
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_tools import WorkTools
from services.orchestrator.loop import Call, Limits, Reservation, Turn
from services.orchestrator.work_tool_selection import WorkToolSelection
from services.runner.machines import Machine, install_command

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFictionalExampleKeyMaterial0000000000000000"
FINGERPRINT = "SHA256:" + "A" * 43
ENV = {"ATLAS_REMOTE_RUNNERS": "1", "ATLAS_PROJECT_MODE": "1"}
CODES = (
    "credential_unavailable",
    "host_key_fingerprint_mismatch",
    "runner_target_address_denied",
    "runner_target_unresolved",
    "runner_transport_failed",
    "runner_connection_unknown",
    "runner_host_key_unavailable",
    "invalid_work_root",
    "dependency_unavailable",
)


class Backend:
    """The member machines routes, per member: absent and not-yours are one answer."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.outcome: dict[str, Any] = dict(status="available")
        self.calls: list[tuple[str, str, str, Any, Any]] = []

    def handle(
        self, member: str, method: str, tail: str, data: Any, revision: Any
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((member, method, tail, data, revision))
        parts = tail.split("/")
        mine = {k: v for k, v in self.rows.items() if v["owner_member_id"] == member}
        if parts == ["machines"] and method == "GET":
            return 200, dict(items=list(mine.values()), next_cursor=None)
        if parts == ["machines"] and method == "POST":
            try:
                fields = Machine.model_validate(data).model_dump()
            except ValidationError:
                return 422, dict(error="invalid_request")
            if any(m["name"] == fields["name"] for m in mine.values()):
                return 409, dict(error="runner_name_conflict")
            identifier = str(uuid4())
            row = dict(
                fields,
                id=identifier,
                owner_member_id=member,
                status="unverified",
                verified_at=None,
                host_key_fingerprints=[],
                public_key=KEY + " atlas",
                revision=1,
                last_probe=None,
                install_command=install_command(
                    fields["user"], fields["host"], fields["port"], KEY
                ),
            )
            self.rows[identifier] = row
            return 201, dict(row)
        machine = mine.get(parts[1]) if len(parts) > 1 else None
        if machine is None:
            return 404, dict(error="not_found")
        if parts[2:] == ["verify"] and method == "POST":
            if self.outcome["status"] == "available":
                machine.update(
                    status="available",
                    verified_at="2026-11-11T10:00:00Z",
                    host_key_fingerprints=[FINGERPRINT],
                )
            else:
                machine["last_probe"] = dict(self.outcome)
            return 200, dict(machine)
        if len(parts) == 2 and method == "PATCH":
            if revision != machine["revision"]:
                return 409, dict(error="revision_conflict")
            machine.update(data, revision=machine["revision"] + 1)
            return 200, dict(machine)
        if len(parts) == 2 and method == "DELETE":
            if revision != machine["revision"]:
                return 409, dict(error="revision_conflict")
            del self.rows[machine["id"]]
            return 200, dict(id=machine["id"], deleted=True, remote_cleanup=False)
        return 404, dict(error="not_found")


class Broker:
    org, project = str(uuid4()), str(uuid4())

    def __init__(self, backend: Backend, member: str) -> None:
        self.backend, self.member_id = backend, member

    async def member(
        self,
        method: str,
        tail: str,
        data: dict[str, Any] | None = None,
        revision: object = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, Any]]:
        return self.backend.handle(self.member_id, method, tail, data, revision)

    async def call(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        assert operation == "events"
        return dict(events=[])


class Execution:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.session = dict(id=str(uuid4()), environment={})
        self.request_id = str(uuid4())

    def fence(self) -> dict[str, Any]:
        return dict(session_id=self.session["id"], request_id=self.request_id)

    async def check(self) -> None:
        return None


def work_tools(
    backend: Backend, member: str = "member-a", *, chat: bool = True, lang: str = "en"
) -> WorkTools:
    tools = WorkTools(MagicMock(), Execution(Broker(backend, member)))
    tools.selection = WorkToolSelection("remote")
    tools.approvals_unattended = chat
    tools.lang = lang
    return tools


def call(tools: WorkTools, tool: str, **arguments: Any) -> dict[str, Any]:
    return dict(
        asyncio.run(tools.execute(Call(str(uuid4()), tool, json.dumps(arguments)), 30))
    )


@patch.dict(os.environ, ENV)
class MachineToolTests(unittest.TestCase):
    def test_add_returns_the_install_command_verbatim_in_auto_mode_in_the_chat(
        self,
    ) -> None:
        backend = Backend()
        tools = work_tools(backend)
        added = call(
            tools, "machine_add", name="build", host="192.0.2.10", user="dev", port=22
        )
        row = next(iter(backend.rows.values()))
        self.assertEqual(added["install_command"], row["install_command"])
        self.assertEqual(
            added["install_command"],
            "ssh dev@192.0.2.10 'mkdir -p ~/.ssh && echo \""
            + KEY
            + " atlas\" >> ~/.ssh/authorized_keys'",
        )
        self.assertEqual(row["permission_mode"], "auto")
        self.assertIn("without asking", added["permission_note"])
        self.assertIn("end your turn", added["next_step"])
        self.assertEqual(tools.machine_commands, {"build": row["install_command"]})
        self.assertNotIn("error", added)
        # Nothing the broker keeps leaks into what the model sees.
        for hidden in ("credential", "private", "owner_member_id", row["id"]):
            self.assertNotIn(hidden, json.dumps(added))

    def test_add_in_a_project_keeps_the_backend_default_mode(self) -> None:
        backend = Backend()
        tools = work_tools(backend, chat=False)
        added = call(
            tools,
            "machine_add",
            name="tests",
            host="198.51.100.7",
            user="ci",
            port=2222,
        )
        self.assertEqual(next(iter(backend.rows.values()))["permission_mode"], "rules")
        self.assertIn("wait for your approval", added["permission_note"])
        self.assertTrue(added["install_command"].startswith("ssh -p 2222 ci@"))

    def test_several_machines_in_one_request(self) -> None:
        backend = Backend()
        tools = work_tools(backend)
        call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        call(tools, "machine_add", name="tests", host="198.51.100.7", user="dev")
        self.assertEqual(sorted(tools.machine_commands), ["build", "tests"])
        listed = call(tools, "machine_list")
        self.assertEqual([m["name"] for m in listed["machines"]], ["build", "tests"])
        self.assertTrue(all(not m["verified"] for m in listed["machines"]))

    def test_hostile_input_is_refused_by_the_backend_and_relayed_as_a_sentence(
        self,
    ) -> None:
        for host, user in (
            ("example.org; id", "dev"),
            ("$(id)", "dev"),
            ("-oProxyCommand=id", "dev"),
            ("example.org", "dev;id"),
            ("a" * 254, "dev"),
            ("example.org", "d" * 65),
        ):
            with self.subTest(host=host, user=user):
                backend = Backend()
                tools = work_tools(backend)
                refused = call(tools, "machine_add", name="x", host=host, user=user)
                self.assertEqual(refused["error"], "invalid_machine")
                self.assertIn("public host name", refused["message"])
                self.assertNotIn(host, refused["message"])
                self.assertNotIn(user, refused["message"])
                self.assertEqual(backend.rows, {})
                self.assertEqual(tools.machine_commands, {})

    def test_verify_in_the_turn_that_declared_never_connects(self) -> None:
        backend = Backend()
        tools = work_tools(backend)
        call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        early = call(tools, "machine_verify", name="build")
        self.assertEqual(early["error"], "awaiting_user")
        self.assertIn("install_command", early)
        self.assertFalse(any(c[2].endswith("/verify") for c in backend.calls))
        # The refusal is the person's next step, not an unresolved failure.
        from services.orchestrator.code_mode import open_failures

        self.assertEqual(open_failures(tools.validation_failures, []), {})

    def test_verify_after_install_enables_remote_commands_in_the_same_turn(
        self,
    ) -> None:
        backend = Backend()
        call(
            work_tools(backend),
            "machine_add",
            name="build",
            host="192.0.2.10",
            user="dev",
        )
        tools = work_tools(backend)
        selection = WorkToolSelection("remote")
        tools.selection = selection
        # A turn where the remote commands were not offered yet (no machine).
        selection.remote_enabled = False
        self.assertNotIn("remote", selection.offered_groups())
        verified = call(tools, "machine_verify", name="build")
        self.assertNotIn("error", verified)
        self.assertIn(FINGERPRINT, verified["message"])
        self.assertTrue(verified["machine"]["verified"])
        self.assertIn('target "build"', verified["next_step"])
        self.assertTrue(selection.remote_enabled)
        self.assertIn("remote", selection.offered_groups())

    def test_every_verification_failure_is_one_sentence_without_its_code(
        self,
    ) -> None:
        for code in CODES:
            with self.subTest(code=code):
                backend = Backend()
                call(
                    work_tools(backend),
                    "machine_add",
                    name="build",
                    host="build.example.org",
                    user="dev",
                )
                backend.outcome = dict(status="unavailable", error=code)
                tools = work_tools(backend, lang="fr")
                result = call(tools, "machine_verify", name="build")
                self.assertEqual(result["error"], code)
                self.assertNotIn(code, result["message"])
                self.assertNotIn("_", result["message"])
                self.assertIn("build", result["message"])
                self.assertIn("never show error codes", result["next_step"])
                if code == "credential_unavailable":
                    self.assertEqual(
                        result["install_command"],
                        next(iter(backend.rows.values()))["install_command"],
                    )
                    self.assertIn("Lancez cette commande", result["message"])
                if code == "host_key_fingerprint_mismatch":
                    self.assertIn("intercepte", result["message"])
                    self.assertIn("reset_host_key=true", result["next_step"])
                if code == "runner_target_address_denied":
                    self.assertIn("réseau privé", result["message"])

    def test_another_members_machine_is_invisible(self) -> None:
        backend = Backend()
        call(
            work_tools(backend),
            "machine_add",
            name="build",
            host="192.0.2.10",
            user="dev",
        )
        other = work_tools(backend, member="member-b")
        self.assertEqual(call(other, "machine_list")["machines"], [])
        for tool, arguments in (
            ("machine_verify", {}),
            ("machine_change", dict(permission_mode="auto")),
            ("machine_forget", dict(confirmed=True)),
        ):
            with self.subTest(tool=tool):
                refused = call(other, tool, name="build", **arguments)
                self.assertEqual(refused["error"], "machine_not_found")
                self.assertIn("no machine yet", refused["message"])
        self.assertEqual(len(backend.rows), 1)
        self.assertTrue(
            all(member == "member-b" for member, *_ in backend.calls[-4:]),
        )

    def test_forget_refuses_without_confirmation_then_deletes(self) -> None:
        backend = Backend()
        tools = work_tools(backend)
        call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        refused = call(tools, "machine_forget", name="build")
        self.assertEqual(refused["error"], "confirmation_required")
        self.assertIn("confirmed=true", refused["message"])
        self.assertEqual(len(backend.rows), 1)
        removed = call(tools, "machine_forget", name="build", confirmed=True)
        self.assertTrue(removed["removed"])
        self.assertIn("authorized_keys", removed["message"])
        self.assertEqual(backend.rows, {})
        delete = [c for c in backend.calls if c[1] == "DELETE"]
        self.assertEqual(delete[0][4], 1)

    def test_change_sends_the_revision_and_explains_the_mode(self) -> None:
        backend = Backend()
        tools = work_tools(backend)
        call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        changed = call(tools, "machine_change", name="Build", permission_mode="rules")
        self.assertEqual(changed["machine"]["permission_mode"], "rules")
        self.assertIn("cannot show yet", changed["permission_note"])
        patch_call = [c for c in backend.calls if c[1] == "PATCH"][0]
        self.assertEqual(patch_call[3], dict(permission_mode="rules"))
        self.assertEqual(patch_call[4], 1)
        empty = call(tools, "machine_change", name="build")
        self.assertEqual(empty["error"], "empty_change")

    def test_a_repeated_add_returns_the_same_command_or_names_the_conflict(
        self,
    ) -> None:
        backend = Backend()
        tools = work_tools(backend)
        first = call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        again = call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        self.assertEqual(again["install_command"], first["install_command"])
        clash = call(tools, "machine_add", name="build", host="203.0.113.5", user="dev")
        self.assertEqual(clash["error"], "runner_name_conflict")
        self.assertIn("dev@192.0.2.10", clash["message"])
        self.assertEqual(len(backend.rows), 1)

    def test_unknown_name_lists_the_known_machines(self) -> None:
        backend = Backend()
        tools = work_tools(backend)
        call(tools, "machine_add", name="build", host="192.0.2.10", user="dev")
        unknown = call(tools, "machine_verify", name="nas")
        self.assertEqual(unknown["error"], "machine_not_found")
        self.assertIn("Your machines: build.", unknown["message"])

    def test_unavailable_service_and_disabled_runners_are_sentences(self) -> None:
        tools = work_tools(Backend())

        async def down(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            return 503, dict(error="dependency_unavailable")

        with patch.object(tools.execution.broker, "member", new=down):
            result = call(tools, "machine_list")
        self.assertIn("unavailable at the moment", result["message"])
        with patch.dict(os.environ, {"ATLAS_REMOTE_RUNNERS": "0"}):
            result = call(tools, "machine_list")
        self.assertEqual(result["error"], "remote_runners_disabled")
        self.assertIn("not enabled", result["message"])

    def test_arguments_are_strict_on_keys_and_lenient_on_the_port(self) -> None:
        backend = Backend()
        tools = work_tools(backend)
        added = call(
            tools, "machine_add", name="build", host="192.0.2.10", user="dev", port="22"
        )
        self.assertNotIn("error", added)
        refused = call(
            tools,
            "machine_add",
            name="b",
            host="192.0.2.11",
            user="dev",
            private_key="x",
        )
        self.assertEqual(refused["error"], "invalid_tool_arguments")
        self.assertEqual(refused["fields"], ["private_key"])


LOCAL_HOSTS = (
    "192.168.0.25",
    "10.0.0.12",
    "172.20.4.9",
    "169.254.10.1",
    "127.0.0.1",
    "100.101.102.103",
    "localhost",
    "nas",
    "atelier-mac.local",
    "Box.Lan",
    "printer.home.arpa",
)
REACHABLE_HOSTS = ("build.example.org", "198.51.100.7", "203.0.113.5", "192.0.2.10")


@patch.dict(os.environ, ENV)
class UnreachableMachineTests(unittest.TestCase):
    """A machine Atlas cannot reach ends the machine attempts of the turn at once.

    Staging, 2026-11-13: a request to work on a machine of the person's home
    network went through about twenty machine and remote calls until the wall
    clock stopped the turn, then asked the person to run the commands himself.
    """

    def test_local_network_hosts_are_recognised_as_written(self) -> None:
        for host in LOCAL_HOSTS:
            with self.subTest(host=host):
                self.assertTrue(machines.local_network(host))
        # IPv6 and hostile text are refused by the backend as invalid input.
        for host in REACHABLE_HOSTS + (
            "$(id)",
            "-oProxyCommand=id",
            "",
            "::1",
            "fe80::1",
        ):
            with self.subTest(host=host):
                self.assertFalse(machines.local_network(host))

    def test_networks_the_operator_made_reachable_are_left_to_the_broker(
        self,
    ) -> None:
        with patch.dict(
            os.environ, {"ATLAS_RUNNER_PERSONAL_NETWORKS": "192.168.0.0/16, bad"}
        ):
            self.assertFalse(machines.local_network("192.168.0.25"))
            self.assertFalse(machines.local_network("nas"))
            self.assertTrue(machines.local_network("10.0.0.12"))

    def test_a_local_address_is_refused_before_any_declaration(self) -> None:
        for lang, words in (("fr", "réseau local"), ("en", "local network")):
            for host in ("192.168.0.25", "gael-macbook.local", "hardy"):
                with self.subTest(lang=lang, host=host):
                    backend = Backend()
                    tools = work_tools(backend, lang=lang)
                    refused = call(
                        tools, "machine_add", name="maison", host=host, user="dev"
                    )
                    self.assertEqual(refused["error"], "machine_local_network")
                    self.assertIn(words, refused["message"])
                    self.assertIn(host, refused["message"])
                    self.assertNotIn("install_command", refused)
                    self.assertIn("own workspace", refused["next_step"])
                    self.assertIn("never give the user commands", refused["next_step"])
                    self.assertIn("never show error codes", refused["next_step"])
                    self.assertEqual(backend.rows, {})
                    self.assertEqual(backend.calls, [])
                    self.assertEqual(tools.machine_commands, {})

    def test_retries_of_an_unreachable_machine_never_reach_the_backend(
        self,
    ) -> None:
        backend = Backend()
        tools = work_tools(backend, lang="fr")
        first = call(tools, "machine_add", name="mac", host="192.168.0.25", user="dev")
        # Another name for the same address, then the same name, then a check.
        for tool, arguments in (
            ("machine_add", dict(name="Mac-Gael", host="192.168.0.25", user="gael")),
            ("machine_add", dict(name="MAC", host="10.0.0.3", user="dev")),
            ("machine_verify", dict(name="mac")),
        ):
            with self.subTest(tool=tool, arguments=arguments):
                again = call(tools, tool, **arguments)
                self.assertEqual(again["error"], "machine_local_network")
                self.assertEqual(again["message"], first["message"])
        self.assertEqual(backend.calls, [])

    def test_a_declared_machine_on_a_local_address_is_never_connected(self) -> None:
        backend = Backend()
        member = "member-a"
        identifier = str(uuid4())
        backend.rows[identifier] = dict(
            id=identifier,
            owner_member_id=member,
            name="atelier",
            host="10.0.0.12",
            user="dev",
            port=22,
            status="unverified",
            verified_at=None,
            host_key_fingerprints=[],
            permission_mode="auto",
            revision=1,
            last_probe=None,
            install_command=install_command("dev", "10.0.0.12", 22, KEY),
        )
        tools = work_tools(backend, member=member)
        result = call(tools, "machine_verify", name="atelier")
        self.assertEqual(result["error"], "machine_local_network")
        self.assertIn("10.0.0.12", result["message"])
        self.assertFalse(any(c[2].endswith("/verify") for c in backend.calls))

    def test_a_machine_that_does_not_answer_is_tried_once_per_turn(self) -> None:
        for code in ("runner_transport_failed", "runner_target_unresolved"):
            with self.subTest(code=code):
                backend = Backend()
                call(
                    work_tools(backend),
                    "machine_add",
                    name="build",
                    host="build.example.org",
                    user="dev",
                )
                backend.outcome = dict(status="unavailable", error=code)
                tools = work_tools(backend)
                first = call(tools, "machine_verify", name="build")
                self.assertEqual(first["error"], code)
                self.assertIn("own workspace", first["next_step"])
                again = call(tools, "machine_verify", name="Build")
                self.assertEqual(again["error"], code)
                verifications = [c for c in backend.calls if c[2].endswith("/verify")]
                self.assertEqual(len(verifications), 1)
                # The remote commands on it answer at once too: the test broker
                # fails on any remote operation.
                remote = call(tools, "remote_run", target="build", command="df -h")
                self.assertEqual(remote["error"], code)
                self.assertEqual(remote["trust"], "untrusted")

    def test_a_refusal_the_person_can_fix_is_not_an_end(self) -> None:
        backend = Backend()
        call(
            work_tools(backend),
            "machine_add",
            name="build",
            host="build.example.org",
            user="dev",
        )
        backend.outcome = dict(status="unavailable", error="credential_unavailable")
        tools = work_tools(backend)
        result = call(tools, "machine_verify", name="build")
        self.assertIn("install_command", result)
        self.assertEqual(tools.machines_unreachable, {})
        call(tools, "machine_verify", name="build")
        verifications = [c for c in backend.calls if c[2].endswith("/verify")]
        self.assertEqual(len(verifications), 2)

    def test_a_guessed_machine_name_is_told_to_ask_instead(self) -> None:
        from services.retrieval.project_identity import ProjectError

        tools = work_tools(Backend())

        async def missing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ProjectError(404, "not_found")

        with patch("services.orchestrator.work_remote_tools.execute", new=missing):
            for target in ("hardy", "Mac de Gael", "mon-mac"):
                with self.subTest(target=target):
                    result = call(tools, "remote_run", target=target, command="ls")
                    self.assertEqual(result["error"], "not_found")
                    self.assertIn("never try other guessed names", result["next_step"])
            status = call(tools, "remote_status", command_id=str(uuid4()))
        self.assertNotIn("guessed", str(status.get("next_step", "")))

    def test_the_rules_name_the_local_network_and_the_own_workspace(self) -> None:
        for unattended in (True, False):
            text = machines.instructions(unattended)
            self.assertIn("local network", text)
            self.assertIn("own workspace", text)
            self.assertIn("Never guess a name for remote_run", text)


class SelectionTests(unittest.TestCase):
    def test_remote_group_offers_the_machine_tools_within_capacity(self) -> None:
        selection = WorkToolSelection("remote")
        selection.reopen_enabled = selection.memory_enabled = True
        selection.skills_enabled = selection.schedules_enabled = True
        for extra in ("load_skill", "propose_scheduled_task"):
            selection.catalog[extra] = dict(type="function", function=dict(name=extra))
        names = selection.names
        self.assertLessEqual(len(names), 16)
        # Connecting the user's own computer is offered only with a relay.
        offered = [n for n in machines.NAMES if n != "machine_connect"]
        for name in (*offered, "list_targets", "remote_run", "remote_status"):
            self.assertIn(name, names)
        self.assertNotIn("machine_connect", names)
        declared = {d["function"]["name"] for d in selection.declarations()}  # type: ignore[index]
        self.assertTrue(set(offered) <= declared)
        relay = {
            "ATLAS_RELAY_PUBLIC_URL": "wss://atlas.example.org/relay/",
            "ATLAS_REMOTE_RUNNERS": "1",
        }
        with patch.dict(os.environ, relay):
            selection = WorkToolSelection("remote")
            selection.reopen_enabled = selection.memory_enabled = True
            selection.skills_enabled = selection.schedules_enabled = True
            for extra in ("load_skill", "propose_scheduled_task"):
                selection.catalog[extra] = dict(
                    type="function", function=dict(name=extra)
                )
            names = selection.names
            self.assertEqual(len(names), 16)
            for name in ("machine_connect", "remote_write_file", "reopen_result"):
                self.assertIn(name, names)
            declared = {d["function"]["name"] for d in selection.declarations()}  # type: ignore[index]
            self.assertIn("remote_write_file", declared)
        for schema in machines.SCHEMAS.values():
            text = json.dumps(schema.model_json_schema())
            self.assertNotIn("private_key", text)
            self.assertNotIn("password", text)

    def test_a_turn_on_a_machine_starts_in_the_remote_group(self) -> None:
        from services.orchestrator.request_router import Decision

        def request(text: str) -> ChatRequest:
            return ChatRequest.model_validate(
                {"messages": [{"role": "user", "content": text}]}
            )

        with patch.dict(os.environ, {"ATLAS_REMOTE_RUNNERS": "1"}):
            compile_there = request("Compile le projet sur mon serveur build")
            self.assertTrue(machines.machine_turn(compile_there, None, False))
            # In a repository session the code group already carries remote_run.
            self.assertFalse(machines.machine_turn(compile_there, None, True))
            self.assertTrue(
                machines.machine_turn(request("x"), Decision(kind="remote"), True)
            )
            self.assertFalse(
                machines.machine_turn(request("x"), Decision(kind="run"), False)
            )
            done = request("c'est fait")
            self.assertFalse(machines.machine_turn(done, None, False))
            done.__dict__["machine_setup_pending"] = True
            self.assertTrue(machines.machine_turn(done, Decision(kind="answer"), False))
        with patch.dict(os.environ, {"ATLAS_REMOTE_RUNNERS": "0"}):
            self.assertFalse(machines.machine_turn(done, None, False))


class AnswerTests(unittest.TestCase):
    COMMAND = (
        "ssh dev@192.0.2.10 'mkdir -p ~/.ssh && echo \""
        + KEY
        + " atlas\" >> ~/.ssh/authorized_keys'"
    )

    def test_a_missing_or_retyped_command_is_added_verbatim(self) -> None:
        answer = "I declared the machine build. Run the command I gave."
        completed = machines.complete_answer(answer, {"build": self.COMMAND}, "en")
        self.assertIn("```bash\n" + self.COMMAND + "\n```", completed)
        self.assertTrue(completed.startswith(answer))
        self.assertTrue(machines.install_shown(completed))
        retyped = "```bash\n" + self.COMMAND.replace("AAAAC3", "AAAAC4") + "\n```"
        self.assertIn(
            "```bash\n" + self.COMMAND + "\n```",
            machines.complete_answer(retyped, {"build": self.COMMAND}, "fr"),
        )

    def test_a_command_already_shown_is_not_repeated(self) -> None:
        shown = "Lancez ceci :\n\n```sh\n" + self.COMMAND + "\n```\n\nPuis dites-moi."
        self.assertEqual(
            machines.complete_answer(shown, {"build": self.COMMAND}, "fr"), shown
        )
        self.assertEqual(machines.complete_answer(shown, {}, "fr"), shown)

    def test_two_missing_commands_are_labelled(self) -> None:
        second = self.COMMAND.replace("192.0.2.10", "198.51.100.7")
        completed = machines.complete_answer(
            "", {"build": self.COMMAND, "tests": second}, "fr"
        )
        self.assertTrue(completed.startswith("Lancez cette commande"))
        self.assertIn("**build**", completed)
        self.assertIn("**tests**", completed)


class Script:
    """A mocked Work model: one scripted turn per call, recording what it saw."""

    def __init__(self, *turns: Turn) -> None:
        self.turns, self.seen = list(turns), []  # type: list[Turn], list[Any]

    def estimate(self, messages: list[dict[str, object]]) -> Reservation:
        return Reservation(1, Decimal(0))

    async def complete(self, messages: list[dict[str, object]], timeout: float) -> Turn:
        self.seen.append([dict(m) for m in messages])
        return self.turns.pop(0)


@patch.dict(os.environ, ENV)
class TwoTurnFlowTests(unittest.IsolatedAsyncioTestCase):
    """Turn 1 declares and shows the command; turn 2 verifies and continues."""

    def limits(self) -> Limits:
        from services.orchestrator.code_mode import code_profile

        return Limits(
            profile=code_profile(), tokens=None, code_mode=True, model_calls=6
        )

    def tools(self, backend: Backend) -> WorkTools:
        tools = work_tools(backend, lang="fr")
        tools.instructions = machines.instructions(True)
        return tools

    async def test_setup_then_original_task(self) -> None:
        from services.orchestrator.chat_work import MACHINE_SETUP_HINT
        from services.orchestrator.code_mode import execute_turn, open_failures

        backend = Backend()
        ask = "J'ai un serveur à 192.0.2.10 (compte dev) : utilise-le en SSH pour compiler le projet avec make."
        model = Script(
            Turn(
                "",
                (
                    Call(
                        "1",
                        "machine_add",
                        json.dumps(dict(name="build", host="192.0.2.10", user="dev")),
                    ),
                ),
            ),
            Turn(
                "La machine build est déclarée. Lancez la commande puis dites-le-moi."
            ),
        )
        tools = self.tools(backend)
        first = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": ask}], "ui_locale": "fr"}
        )
        result = await execute_turn(first, model, tools, self.limits())
        self.assertEqual(result.state, "done")
        self.assertEqual(open_failures(tools.validation_failures, []), {})
        system = str(model.seen[0][0]["content"])
        self.assertIn("Never ask the user for a password", system)
        self.assertIn("permission mode auto", system)
        # As process_work does: the command reaches the person, verbatim.
        answer = machines.complete_answer(
            result.text, tools.machine_commands, first.lang
        )
        command = next(iter(backend.rows.values()))["install_command"]
        self.assertIn("```bash\n" + command + "\n```", answer)
        self.assertFalse(any(c[2].endswith("/verify") for c in backend.calls))

        # The person ran the command in his terminal; turn 2 says so.
        backend.outcome = dict(status="available")
        ran: list[dict[str, Any]] = []

        async def remote(
            tools: Any, name: str, arguments: str, timeout: float
        ) -> dict[str, Any]:
            ran.append(dict(name=name, **json.loads(arguments)))
            return dict(state="succeeded", exit_code=0, output="build ok")

        model = Script(
            Turn("", (Call("2", "machine_verify", json.dumps(dict(name="build"))),)),
            Turn(
                "",
                (
                    Call(
                        "3",
                        "remote_run",
                        json.dumps(dict(target="build", command="make", wait_s=60)),
                    ),
                ),
            ),
            Turn("La compilation sur build a réussi : build ok."),
        )
        tools = self.tools(backend)
        second = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": ask},
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": "c'est fait" + MACHINE_SETUP_HINT},
                ],
                "ui_locale": "fr",
            }
        )
        with patch("services.orchestrator.work_remote_tools.execute", new=remote):
            result = await execute_turn(second, model, tools, self.limits())
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, "La compilation sur build a réussi : build ok.")
        self.assertEqual(
            ran, [dict(name="remote_run", target="build", command="make", wait_s=60)]
        )
        verify = [c for c in backend.calls if c[2].endswith("/verify")]
        self.assertEqual(len(verify), 1)
        self.assertEqual(tools.machine_commands, {})
        self.assertEqual(open_failures(tools.validation_failures, []), {})
        # The model saw the earlier turns, so it continued without asking again.
        self.assertTrue(any(ask in str(m.get("content")) for m in model.seen[0]))


class ChatFollowUpTests(unittest.IsolatedAsyncioTestCase):
    """The reply to "run this command, then tell me" goes back to the same Work session."""

    ORG = "123e4567-e89b-12d3-a456-426614174000"
    PROJECT = "123e4567-e89b-12d3-a456-426614174002"
    SESSION = "123e4567-e89b-12d3-a456-426614174004"
    SHOWN = (
        "Lancez cette commande :\n\n```bash\nssh dev@192.0.2.10 'mkdir -p ~/.ssh && echo \""
        + KEY
        + " atlas\" >> ~/.ssh/authorized_keys'\n```"
    )

    def request(self, reply: str, shown: str | None = None) -> ChatRequest:
        value = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Compile sur mon serveur 192.0.2.10"},
                    {"role": "assistant", "content": shown or self.SHOWN},
                    {"role": "user", "content": reply},
                ],
                "ui_locale": "fr",
            }
        )
        value.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        value.__dict__["artifact_owner"] = "member@example.org"
        return value

    async def escalate(
        self, request: ChatRequest, routed: Any = None
    ) -> tuple[bool, list[Any]]:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.request_router import DECISION_KEY

        if routed is not None:
            request.__dict__[DECISION_KEY] = routed
        calls: list[Any] = []

        async def fake_process_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((r, kwargs))
            i.reponse, i.state = "verified and compiled", "done"

        environment = {"ATLAS_CHAT_WORK": "1", "ATLAS_REMOTE_RUNNERS": "1"}
        environment["ATLAS_MODEL_ROUTER"] = "1" if routed is not None else "0"
        bound = dict(conversation_id=str(uuid4()), work_session_id=self.SESSION)
        with (
            patch.dict(os.environ, environment),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(return_value={"memberships": [{"org_id": self.ORG}]}),
            ),
            patch(
                "services.orchestrator.chat_work._find_scratch_project",
                new=AsyncMock(return_value=self.PROJECT),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(return_value=self.PROJECT),
            ),
            patch(
                "services.orchestrator.chat_work._find_binding",
                new=AsyncMock(return_value=bound),
            ),
            patch(
                "services.orchestrator.chat_work._create_conversation",
                new=AsyncMock(side_effect=AssertionError("must not be called")),
            ),
            patch(
                "services.orchestrator.code_mode.process_work", new=fake_process_work
            ),
        ):
            handled = await escalate_to_work(request, Interaction())
        return handled, calls

    async def test_any_confirmation_goes_back_to_the_bound_session(self) -> None:
        from services.orchestrator.chat_work import MACHINE_SETUP_HINT
        from services.orchestrator.request_router import Decision

        for reply, routed in (
            ("c'est fait", None),
            ("ok j'ai collé la commande", None),
            ("done", Decision(kind="answer")),
            ("voilà", Decision(kind="other")),
            ("c'est bon", Decision(kind="remote", follow_up=True)),
        ):
            with self.subTest(reply=reply, routed=routed):
                handled, calls = await self.escalate(self.request(reply), routed)
                self.assertTrue(handled)
                scoped, kwargs = calls[0]
                self.assertEqual(kwargs["session_id"], self.SESSION)
                self.assertTrue(scoped.__dict__["machine_setup_pending"])
                self.assertEqual(
                    scoped.messages[-1].content, reply + MACHINE_SETUP_HINT
                )
                # The original request and the shown command come along.
                self.assertIn("Compile sur mon serveur", scoped.messages[0].content)

    async def test_a_new_request_of_another_kind_leaves_the_setup(self) -> None:
        from services.orchestrator.request_router import Decision

        handled, calls = await self.escalate(
            self.request("quel temps fera-t-il demain ?"), Decision(kind="research")
        )
        self.assertFalse(handled)
        self.assertEqual(calls, [])

    async def test_without_a_shown_command_a_short_reply_stays_in_the_chat(
        self,
    ) -> None:
        handled, calls = await self.escalate(
            self.request("c'est fait", shown="Voici une explication.")
        )
        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_the_router_sees_the_pending_setup(self) -> None:
        from services.orchestrator.request_router import messages

        routed = messages("c'est fait", [{"role": "assistant", "content": self.SHOWN}])
        self.assertIn("set up the user's machine", str(routed[1]["content"]))


class MemberRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_calls_the_org_route_as_the_member(self) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        from services.orchestrator.code_api import ExecutionBroker

        seen: list[Any] = []

        async def handler(request: web.Request) -> web.Response:
            seen.append(
                (
                    request.method,
                    request.path,
                    dict(request.headers),
                    await request.text(),
                )
            )
            return web.json_response(dict(items=[]))

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        identity = AsyncMock(return_value=dict(identity="signed-identity"))
        broker = ExecutionBroker("member@example.org", Broker.org, Broker.project)
        with (
            patch("services.orchestrator.project_ui.broker_identity", new=identity),
            patch(
                "services.orchestrator.chat_pipeline.retrieval_url",
                return_value=str(server.make_url("")).rstrip("/"),
            ),
        ):
            status, body = await broker.member("GET", "machines")
            await broker.member("PATCH", "machines/x", dict(name="b"), revision=3)
        self.assertEqual((status, body), (200, dict(items=[])))
        # Resolved without a project: the machines routes are the member's own.
        self.assertEqual(
            identity.await_args_list[0].args[1:],
            ("member@example.org", Broker.org, None),
        )
        method, path, headers, _ = seen[0]
        self.assertEqual(
            (method, path), ("GET", f"/api/atlas/orgs/{Broker.org}/machines")
        )
        self.assertEqual(headers["X-Atlas-Project-Identity"], "signed-identity")
        self.assertNotIn("Idempotency-Key", headers)
        method, path, headers, text = seen[1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(headers["If-Match"], '"3"')
        self.assertTrue(headers["Idempotency-Key"])
        self.assertEqual(json.loads(text), dict(name="b"))


if __name__ == "__main__":
    unittest.main()
