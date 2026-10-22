"""Offline Code mode boundaries and fake transcript through the real chat pipeline."""

import asyncio
import base64
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import zipfile

from pydantic import ValidationError

from packages.profiles import PROFILES
from services.orchestrator import code_mode, code_worker
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.code_tools import ApprovalGate, CodeTools
from services.orchestrator.code_workspace import Audit, Workspace, enabled, redact
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Message, Reservation, Turn


def archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as stream:
        for name, content in files.items():
            stream.writestr(name, content)
    return output.getvalue()


class LocalWorkspace(Workspace):
    root: Path

    async def prepare(self) -> None:
        self.root.mkdir(exist_ok=True)

    async def invoke(
        self, action: str, args: dict[str, object], timeout: float
    ) -> dict[str, object]:
        return await asyncio.to_thread(code_worker.perform, self.root, action, args)


class FakeModel:
    def __init__(self, turns: list[Turn]) -> None:
        self.turns = iter(turns)
        self.tools: list[dict[str, object]] = []
        self.sink = None
        self.input_tokens = self.output_tokens = 0
        self.spent = Decimal(0)
        self.provider_model = ""
        self.reasoning_retried = False
        self.messages: list[Message] = []

    def configure_quality(self, *args: object, **kwargs: object) -> None:
        pass

    def estimate(self, messages: list[Message]) -> Reservation:
        return Reservation(1, Decimal("0.01"))

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.messages = messages
        return next(self.turns)


class CodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.env = patch.dict(
            os.environ,
            {
                "ATLAS_CODE_MODE": "1",
                "ATLAS_STABLE": "0",
                "ATLAS_PORT_BASE": "5900",
                "ATLAS_CODE_STATE_DIR": str(Path(self.temp.name) / "state"),
            },
        )
        self.env.start()
        self.session = str(uuid4())
        self.workspace = LocalWorkspace(self.session)
        self.workspace.root = self.root
        self.audit = Audit(self.session, "test-turn")
        self.tools = CodeTools(self.workspace, self.audit)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def request(self, **updates: object) -> ChatRequest:
        return ChatRequest.model_validate(
            {
                "model": PROFILES[0],
                "code_mode": True,
                "code_session_id": self.session,
                "messages": [
                    {"role": "user", "content": "Fix the failing test in this project."}
                ],
                "lang": "en",
                "ui_locale": "en",
                **updates,
            }
        )

    def test_archive_traversal_absolute_links_and_size(self) -> None:
        for name in (
            "../escape",
            "/absolute",
            "/workspace/escape",
            "dir/../../escape",
            "a\\b",
            ".git/config",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                code_worker.extract(archive({name: "bad"}), self.root)
        for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as tar:
                info = tarfile.TarInfo("link")
                info.type, info.linkname = link_type, "../../outside"
                tar.addfile(info)
            with self.assertRaises(ValueError):
                code_worker.extract(stream.getvalue(), self.root)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            zip_info = zipfile.ZipInfo("link")
            zip_info.create_system = 3
            zip_info.external_attr = 0o120777 << 16
            zipped.writestr(zip_info, "../../outside")
        with self.assertRaises(ValueError):
            code_worker.extract(stream.getvalue(), self.root)
        with (
            patch.object(code_worker, "MAX_BYTES", 10),
            self.assertRaisesRegex(ValueError, "bytes 11; limit 10"),
        ):
            code_worker.extract(archive({"a": "x" * 11}), self.root)
        with (
            patch.object(code_worker, "MAX_FILES", 1),
            self.assertRaisesRegex(ValueError, "entries 2; limit 1"),
        ):
            code_worker.extract(archive({"a": "", "b": ""}), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_import_baseline_export_and_uniqueness(self) -> None:
        baseline = code_worker.perform(
            self.root,
            "import",
            {"archive": base64.b64encode(archive({"a.py": "same same\n"})).decode()},
        )
        for text in ("missing", "same", ""):
            with self.assertRaisesRegex(ValueError, "exactly_once"):
                code_worker.perform(
                    self.root,
                    "edit_file",
                    {"path": "a.py", "old_text": text, "new_text": "fixed"},
                )
        result = code_worker.perform(
            self.root,
            "edit_file",
            {"path": "a.py", "old_text": "same same", "new_text": "fixed"},
        )
        self.assertIn("+fixed", result["diff"])
        absolute = code_worker.perform(
            self.root, "read_file", {"path": "/workspace/a.py"}
        )
        self.assertIn("fixed", absolute["output"])
        with self.assertRaises(ValueError):
            code_worker.perform(
                self.root, "read_file", {"path": "/workspace/../etc/passwd"}
            )
        code_worker.perform(
            self.root, "write_file", {"path": "untracked.txt", "content": "new content"}
        )
        code_worker.perform(
            self.root,
            "write_file",
            {"path": "whitespace.txt", "content": "trailing  \n"},
        )
        exported = code_worker.perform(self.root, "export", baseline)
        self.assertIn("+fixed", exported["patch"])
        self.assertIn("+new content", exported["patch"])
        self.assertIn("+trailing  \n", exported["patch"])
        self.assertTrue(base64.b64decode(exported["archive"]))
        (self.root / "link").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            code_worker.perform(self.root, "read_file", {"path": "link"})
        with self.assertRaises(ValueError):
            code_worker.perform(self.root, "export", baseline)

    async def test_command_limits_and_exit_codes(self) -> None:
        result = await self.workspace.invoke(
            "run_command", {"command": "python3 -c 'print(\"x\" * 50000)'"}, 5
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(str(result["output"])), code_worker.OUTPUT_CAP)
        result = await self.workspace.invoke(
            "run_command", {"command": "sleep 5", "timeout_seconds": 0.05}, 5
        )
        self.assertTrue(result["timed_out"])
        self.assertLess(float(str(result["elapsed_seconds"])), 1)
        result = await self.workspace.invoke(
            "run_command",
            {"command": "python3 -c 'a=bytearray(256*1024*1024)'", "memory_mb": 32},
            5,
        )
        self.assertNotEqual(result["exit_code"], 0)
        result = await self.workspace.invoke("run_command", {"command": "exit 7"}, 5)
        self.assertEqual(result["exit_code"], 7)

    async def test_gate_sensitive_action_allow_deny_timeout(self) -> None:
        gate = ApprovalGate()
        for choice in (True, False, None):
            actions: list[str] = []
            events: list[dict[str, object]] = []

            async def sink(event: dict[str, object]) -> None:
                events.append(event)
                data = event["code"]
                assert isinstance(data, dict)
                output = data["output"]
                assert isinstance(output, dict)
                if data["status"] == "running" and choice is not None:
                    self.assertTrue(
                        await asyncio.to_thread(
                            gate.answer, str(output["approval_id"]), choice
                        )
                    )
                    self.assertFalse(gate.answer(str(output["approval_id"]), choice))

            self.tools.sink = sink

            async def sensitive() -> str:
                decision = await gate.wait("Fake sensitive action", 0.02, self.tools)
                if decision == "allowed":
                    actions.append("executed")
                return decision

            decision = await sensitive()
            self.assertEqual(actions, ["executed"] if choice else [])
            self.assertEqual(
                decision,
                "allowed" if choice else "denied" if choice is False else "timeout",
            )
            self.assertEqual(len(events), 2)
            self.assertFalse(gate.pending)

    async def test_audit_and_tool_results_redact_credentials(self) -> None:
        secret = "synthetic-test-credential-value"
        with patch.dict(os.environ, {"EXAMPLE_API_KEY": secret}):
            (self.root / "sample").write_text(secret)
            result = await self.tools.execute(
                Call("read", "read_file", '{"path":"sample"}'), 5
            )
            self.assertNotIn(secret, json.dumps(result))
            await self.tools.execute(
                Call(
                    "write",
                    "write_file",
                    json.dumps({"path": "new", "content": "password=never-expose-me"}),
                ),
                5,
            )
            audit = self.audit.path.read_text()
            self.assertNotIn(secret, audit)
            self.assertNotIn("never-expose-me", audit)
            self.assertIn("read_file", audit)
            self.assertIn("diff", audit)
            self.assertEqual(self.audit.path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(secret, str(redact({"nested": [secret]})))
            self.assertNotIn("fixture-value", redact("API_TOKEN=fixture-value"))
            self.assertEqual(
                redact({"apiKey": "fixture-value", "token": "fixture-value"}),
                {"apiKey": "[REDACTED]", "token": "[REDACTED]"},
            )
            self.assertNotIn(
                "Zml4dHVyZTp2YWx1ZQ==", redact("Basic Zml4dHVyZTp2YWx1ZQ==")
            )
            self.assertNotIn("fixture-value", redact('{"api_key": "fixture-value"}'))
            self.assertNotIn(
                "fixture-value",
                redact(json.dumps({"content": '{"api_key": "fixture-value"}'})),
            )
            self.assertEqual(
                redact({"password": "fixture-value"}), {"password": "[REDACTED]"}
            )
            self.assertNotIn(
                "fixture-value", redact("https://user:fixture-value@example.org")
            )

    def test_honesty_five_languages(self) -> None:
        claims = {
            "en": "All tests passed.",
            "fr": "Tous les tests sont réussis.",
            "de": "Alle Tests haben bestanden.",
            "es": "Todas las pruebas pasaron.",
            "it": "Tutti i test sono superati.",
        }
        for lang, claim in claims.items():
            corrected, changed = code_mode.honest(claim, [], lang)
            self.assertTrue(changed, lang)
            self.assertNotIn(claim, corrected)
            self.assertEqual(
                code_mode.honest(claim, [{"exit_code": 0}], lang), (claim, False)
            )
        for text in (
            "Tests did not pass.",
            "Les tests ne sont pas réussis.",
            "Die Tests haben nicht bestanden.",
            "Las pruebas no pasaron.",
            "I test non sono superati.",
        ):
            self.assertFalse(code_mode.honest(text, [], "en")[1])
        self.assertEqual(
            code_mode.honest("I changed the function.", [], "en"),
            ("I changed the function.", False),
        )

    def test_flag_off_and_stable_off(self) -> None:
        for values in ({"ATLAS_CODE_MODE": "0"}, {"ATLAS_STABLE": "1"}):
            with patch.dict(os.environ, values):
                self.assertFalse(enabled())
                with self.assertRaises(ValidationError):
                    self.request()
                ordinary = ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": "Hello"}]}
                )
                self.assertFalse(ordinary.code_mode)
                self.assertEqual(ordinary.timeout_seconds, 120)

    async def test_quotas_report_measured_values(self) -> None:
        from dataclasses import replace

        cases = [
            (
                replace(code_mode.settings(), tool_calls=0),
                Turn("", (Call("x", "list_dir", "{}"),)),
                "tool_calls",
                "0",
            ),
            (
                replace(code_mode.settings(), cost=Decimal("0.001")),
                Turn("answer"),
                "cost",
                "0.001",
            ),
            (
                replace(code_mode.settings(), wall_clock=0),
                Turn("answer"),
                "wall_clock",
                "0",
            ),
            (
                replace(code_mode.settings(), model_calls=0),
                Turn("answer"),
                "model_calls",
                "0",
            ),
        ]
        for limits, turn, reason, limit in cases:
            result = await code_mode.execute_turn(
                self.request(), FakeModel([turn]), self.tools, limits
            )
            self.assertEqual(result.reason, reason)
            self.assertIn("limit " + limit, result.text)
            self.assertNotIn("504", result.text)
            event = json.loads(self.audit.path.read_text().splitlines()[-1])
            self.assertEqual(event["type"], "limit")
            self.assertIn("measured", event["output"])

    def test_tiny_measured_limit_is_fixed_decimal(self) -> None:
        from services.orchestrator.loop import display_limit_value

        self.assertEqual(display_limit_value(9.159100000033504e-05), "0.000")
        self.assertEqual(display_limit_value(1.23456), "1.235")
        self.assertEqual(display_limit_value(9), "9")

    async def test_plan_persists_and_echo_is_not_test_evidence(self) -> None:
        plan = {"items": [{"text": "Repair", "status": "in_progress"}]}
        result = await self.tools.execute(
            Call("plan", "update_plan", json.dumps(plan)), 5
        )
        self.assertEqual(result, plan)
        self.assertEqual(self.workspace.metadata()["plan"], plan["items"])
        await self.tools.execute(
            Call("echo", "run_command", json.dumps({"command": "echo tests passed"})), 5
        )
        self.assertFalse(self.tools.test_evidence)
        self.assertTrue(
            code_mode.honest("Tests passed", self.tools.test_evidence, "en")[1]
        )

    async def test_public_upload_normalization_and_disabled_routes(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        archive_data = base64.b64encode(archive({"sample": "content"})).decode()
        observed: list[ChatRequest] = []

        async def capture(request: ChatRequest, item: Interaction) -> None:
            observed.append(request)
            item.reponse, item.state = "Archive received", "done"

        payload = {
            "model": PROFILES[0],
            "code_mode": True,
            "code_session_id": self.session,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Repair this project"},
                        {
                            "type": "file",
                            "file": {
                                "filename": "project.zip",
                                "file_data": archive_data,
                            },
                        },
                    ],
                }
            ],
        }
        with (
            patch("services.orchestrator.chat_api.process", capture),
            patch("services.orchestrator.chat_api.write_interaction"),
            patch.dict(os.environ, {"ATLAS_DATA_DIR": self.temp.name}),
        ):
            with TestClient(app) as client:
                result = client.post("/v1/chat/completions", json=payload)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(observed[0].documents[0].file_data, archive_data)
                self.audit.record("test", output={"safe": True})
                self.assertEqual(
                    client.get(f"/code/sessions/{self.session}/audit").status_code, 200
                )
                with patch.dict(os.environ, {"ATLAS_STABLE": "1"}):
                    self.assertEqual(
                        client.get(f"/code/sessions/{self.session}/audit").status_code,
                        404,
                    )
                    self.assertEqual(
                        client.post(
                            "/code/approvals/invalid", json={"allow": True}
                        ).status_code,
                        404,
                    )
                    self.assertEqual(
                        client.post("/v1/chat/completions", json=payload).status_code,
                        400,
                    )

    async def test_whole_task_replay_real_pipeline_and_stream(self) -> None:
        files = {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        }
        raw = archive(files)
        request = self.request(
            documents=[
                {"filename": "project.zip", "file_data": base64.b64encode(raw).decode()}
            ],
            stream=True,
        )
        model = FakeModel(
            [
                Turn(
                    "",
                    (
                        Call("1", "read_file", '{"path":"calc.py"}'),
                        Call(
                            "2", "run_command", '{"command":"python3 -m unittest -v"}'
                        ),
                    ),
                ),
                Turn(
                    "",
                    (
                        Call(
                            "3",
                            "edit_file",
                            '{"path":"calc.py","old_text":"a - b","new_text":"a + b"}',
                        ),
                    ),
                ),
                Turn(
                    "",
                    (Call("4", "run_command", '{"command":"python3 -m unittest -v"}'),),
                ),
                Turn(
                    "The addition function is fixed. All tests passed.",
                    usage=Reservation(1, Decimal("0.001")),
                ),
            ]
        )
        terminal = AsyncMock()
        terminal.upload_generated.side_effect = (
            lambda filename,
            content,
            timeout,
            **kwargs: "/home/user/requests/chatcmpl-test/" + filename
        )
        item = Interaction()
        with (
            patch.object(code_mode, "Workspace", return_value=self.workspace),
            patch.object(code_mode.CodeModel, "connect", AsyncMock(return_value=model)),
            patch.object(code_mode, "TerminalClient", return_value=terminal),
            patch("services.orchestrator.chat_stream.write_interaction"),
        ):
            stream = response(request, item, __import__("time").monotonic(), process)
            chunks = [str(chunk) async for chunk in stream.body_iterator]
        wire = "".join(chunks)
        self.assertIn("All tests passed.", item.reponse)
        self.assertIn("workspace.tar.gz", item.reponse)
        self.assertIn("changes.patch", item.reponse)
        self.assertIn("<details>", wire)
        self.assertIn('"code":', wire)
        self.assertNotIn('"error": {"code": "stream_error"', wire)
        self.assertIn("a + b", (self.root / "calc.py").read_text())
        self.assertTrue(
            any(
                '"archive_state": "already_extracted"' in str(message.get("content"))
                and "calc.py" in str(message.get("content"))
                for message in model.messages
            )
        )
        events = (
            [json.loads(line) for line in self.audit.path.read_text().splitlines()]
            if self.audit.path.exists()
            else []
        )
        # Pipeline records under its real request id in the same session audit.
        finished = [
            event
            for event in events
            if event["type"] == "tool"
            and event["status"] != "running"
            and event["tool"] == "run_command"
        ]
        self.assertEqual([event["output"]["exit_code"] for event in finished], [1, 0])
        recap = next(event for event in events if event["type"] == "recap")
        self.assertEqual(recap["output"]["steps"], 4)
        self.assertEqual(terminal.upload_generated.await_count, 2)


if __name__ == "__main__":
    unittest.main()
