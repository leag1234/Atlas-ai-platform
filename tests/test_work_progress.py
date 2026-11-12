"""A chat turn handed to Work shows its steps as localized status updates.

Staging corpus 2026-11-12: document turns run in Work showed "Thinking…" for
72 to 204 seconds, then the answer, while Work reported every step as a
project event the chat interface does not render.
"""

import json
import os
import re
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator import work_progress
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.code_tools import WorkTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.stream_client import sink_context
from services.orchestrator.work_progress import LABELS, WorkProgress, label

SECRET_PATH = "dossier/Claire-Dubois-lettre.docx"
SECRET_COMMAND = "pandoc lettre.md -o lettre.docx"


def project(seq: int, kind: str, tool: str | None, status: str) -> dict[str, Any]:
    # The label and output carry content the status must never show.
    return {
        "project": {
            "seq": seq,
            "type": kind,
            "tool": tool,
            "status": status,
            "label": "Changes: " + SECRET_PATH,
            "output": {"tool_result": SECRET_COMMAND},
        }
    }


class WorkProgressMappingTests(unittest.TestCase):
    def test_every_language_has_every_label(self) -> None:
        keys = set(LABELS["en"])
        self.assertEqual(set(LABELS), {"fr", "en", "de", "es", "it"})
        for language, table in LABELS.items():
            with self.subTest(language=language):
                self.assertEqual(set(table), keys)
                if language != "en":
                    for key in keys:
                        self.assertNotEqual(table[key], LABELS["en"][key], key)
                for text in table.values():
                    self.assertNotRegex(text, r"[a-z]+_[a-z]+")

    def test_a_document_turn_sequence(self) -> None:
        progress = WorkProgress("fr", lambda: True)
        steps = [
            progress.step({"type": "workspace", "status": "completed"}),
            progress.step({"type": "tool", "tool": "web_search", "status": "pending"}),
            progress.step(
                {"type": "tool", "tool": "web_search", "status": "completed"}
            ),
            progress.step(
                {"type": "tool", "tool": "switch_tool_group", "status": "completed"}
            ),
            progress.step(
                {"type": "tool", "tool": "publish_document", "status": "running"}
            ),
            progress.step(
                {"type": "tool", "tool": "publish_document", "status": "failed"}
            ),
            progress.step(
                {"type": "tool", "tool": "publish_document", "status": "completed"}
            ),
            progress.step({"type": "recap", "status": "completed"}),
        ]
        self.assertEqual(
            steps,
            [
                ("work_step", "Préparation du document…"),
                ("web_search", ""),
                ("work_step", "Préparation du document…"),
                None,
                ("work_step", "Publication du fichier…"),
                ("work_step", "Préparation du document…"),
                ("work_step", "Vérification du résultat…"),
                None,
            ],
        )

    def test_code_and_machine_turns(self) -> None:
        progress = WorkProgress("de", lambda: False)
        self.assertEqual(
            progress.step({"type": "tool", "tool": "bash", "status": "running"}),
            ("work_step", "Befehl ausführen…"),
        )
        self.assertEqual(
            progress.step({"type": "tool", "tool": "bash", "status": "completed"}),
            ("thinking", ""),
        )
        self.assertEqual(
            progress.step({"type": "tool", "tool": "write", "status": "pending"}),
            ("work_step", "Datei schreiben…"),
        )
        self.assertEqual(
            progress.step(
                {"type": "tool", "tool": "machine_verify", "status": "pending"}
            ),
            ("work_step", "Arbeit auf dem Rechner…"),
        )
        # A tool this table does not know is a generic step, never its name.
        self.assertEqual(
            progress.step(
                {"type": "tool", "tool": "brand_new_tool", "status": "running"}
            ),
            ("work_step", "In Arbeit…"),
        )
        self.assertEqual(label("nl", "planning"), LABELS["en"]["planning"])

    def test_the_workspace_is_prepared_until_the_first_step(self) -> None:
        # Staging corpus 2026-11-12: 17 to 31 s of "Thinking…" before the first
        # step of a code or machine turn.
        progress = WorkProgress("fr", lambda: False)
        self.assertEqual(
            progress.phase("preparing"),
            ("work_step", "Préparation de l’espace de travail…"),
        )
        # The engine still starts after the workspace event: the status stays.
        self.assertIsNone(progress.step({"type": "workspace", "status": "completed"}))
        self.assertIsNone(
            progress.step(
                {"type": "tool", "tool": "switch_tool_group", "status": "completed"}
            )
        )
        self.assertEqual(
            progress.step({"type": "tool", "tool": "bash", "status": "pending"}),
            ("work_step", "Exécution d’une commande…"),
        )
        self.assertEqual(
            progress.step({"type": "tool", "tool": "bash", "status": "completed"}),
            ("thinking", ""),
        )
        self.assertIsNone(progress.phase("unknown"))

    def test_a_document_turn_prepares_the_document_once_the_workspace_is_ready(
        self,
    ) -> None:
        progress = WorkProgress("en", lambda: True)
        self.assertEqual(
            progress.phase("preparing"), ("work_step", "Preparing the workspace…")
        )
        self.assertEqual(
            progress.step({"type": "workspace", "status": "completed"}),
            ("work_step", "Preparing the document…"),
        )

    def test_a_declined_hand_over_goes_back_to_thinking(self) -> None:
        progress = WorkProgress("es", lambda: False)
        progress.phase("preparing")
        self.assertEqual(progress.phase("declined"), ("thinking", ""))
        self.assertEqual(
            progress.step({"type": "workspace", "status": "completed"}),
            ("thinking", ""),
        )

    def test_throttle(self) -> None:
        progress = WorkProgress("en", lambda: False)
        self.assertTrue(progress.due(100.0))
        self.assertFalse(progress.due(100.4))
        self.assertTrue(progress.due(101.1))


class WorkToolsAnnounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_call_is_announced_without_its_arguments(self) -> None:
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        execution = MagicMock()
        execution.check = AsyncMock()
        tools = WorkTools(MagicMock(), execution, sink)
        tools.web = None
        with patch.object(tools, "emit", new=AsyncMock()):
            await tools.execute(
                Call("fetch", "web_fetch", json.dumps({"url": "https://a.test/x"})), 1
            )
        self.assertEqual(events, [{"work_step": {"tool": "web_fetch"}}])


class OpencodePendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_call_being_written_is_announced_once(self) -> None:
        from typing import cast

        from services.orchestrator import work_opencode_turn as turn_module
        from test_work_opencode_turn import _Item, _Limits, _Request, _Tools, _patched

        announced: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            announced.append(event)

        tools = _Tools()
        setattr(tools, "sink", sink)
        events = [
            {"kind": "tool", "tool": "write", "status": "pending", "input": ""},
            {"kind": "tool", "tool": "write", "status": "pending", "input": ""},
            {"kind": "tool", "tool": "write", "status": "running", "input": "a.py"},
            {"kind": "tool", "tool": "write", "status": "completed", "input": "a.py"},
        ]
        start_mock, stop_mock, driver_cls, driver = _patched()

        async def run(prompt: str) -> dict[str, Any]:
            emit = driver_cls_await.call_args.args[2]
            for event in events:
                await emit(event)
            return {"outcome": "complete", "events": events}

        driver.run = AsyncMock(side_effect=run)
        with (
            start_mock,
            stop_mock,
            driver_cls as driver_cls_await,
            patch.object(turn_module, "emit_changes", AsyncMock()),
        ):
            await turn_module.run_opencode_turn(
                cast(Any, _Request()),
                cast(Any, _Item()),
                cast(Any, tools),
                cast(Any, MagicMock()),
                cast(Any, _Limits()),
                True,
            )
        self.assertEqual(announced, [{"work_step": {"tool": "write"}}])
        # Persisted events are unchanged: no pending step is recorded.
        self.assertEqual(
            [(e["tool"], e["status"]) for e in tools.events],
            [("write", "running"), ("write", "completed")],
        )


class ChatStreamRelayTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, text: str, locale: str = "fr", **extra: Any) -> ChatRequest:
        return ChatRequest.model_validate(
            {
                "model": "atlas-qwen",
                "atlas_ui": True,
                "stream": True,
                "ui_locale": locale,
                "messages": [{"role": "user", "content": text}],
                **extra,
            }
        )

    async def statuses(
        self, payload: ChatRequest, events: list[dict[str, Any]], stable: bool
    ) -> tuple[list[str], list[dict[str, Any]]]:
        async def work(request: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            for event in events:
                await sink(event)
            item.reponse, item.state = "Voici la lettre.", "done"
            await sink({"delta": {"content": item.reponse}})

        environment = {"ATLAS_STABLE": "1"} if stable else {}
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root, **environment}),
        ):
            if not stable:
                os.environ.pop("ATLAS_STABLE", None)
            stream = response(payload, Interaction(), time.monotonic(), work)
            rows = [
                json.loads(str(line).removeprefix("data: "))
                async for line in stream.body_iterator
                if str(line).startswith("data: {")
            ]
        found: list[str] = []
        for row in rows:
            description = ((row.get("event") or {}).get("data") or {}).get(
                "description"
            )
            if description:
                description = re.sub(r"\s*\(\d+\.\d+s / \d+s\)$", "", description)
                description = re.sub(r"\s*\d+\s*s$", "", description)
                if not found or found[-1] != description:
                    found.append(description)
        return found, rows

    DOCUMENT_TURN = [
        project(1, "workspace", None, "completed"),
        {"work_step": {"tool": "update_plan"}},
        project(2, "tool", "update_plan", "running"),
        project(3, "plan", "update_plan", "completed"),
        project(4, "tool", "update_plan", "completed"),
        {"work_step": {"tool": "publish_document"}},
        project(5, "tool", "publish_document", "running"),
        project(6, "tool", "publish_document", "completed"),
        project(7, "recap", None, "completed"),
    ]

    async def test_french_document_turn_shows_each_step(self) -> None:
        payload = self.payload("Rédige une lettre de motivation au format docx")
        for stable in (True, False):
            with (
                self.subTest(stable=stable),
                patch.object(work_progress, "MINIMUM_INTERVAL_SECONDS", 0.0),
            ):
                found, rows = await self.statuses(payload, self.DOCUMENT_TURN, stable)
                self.assertEqual(
                    found,
                    [
                        "Réflexion…",
                        "Préparation du document…",
                        "Planification des étapes…",
                        "Préparation du document…",
                        "Publication du fichier…",
                        "Vérification du résultat…",
                        "Réponse terminée",
                    ],
                )
                text = json.dumps(rows, ensure_ascii=False)
                for hidden in (SECRET_PATH, SECRET_COMMAND, "publish_document"):
                    self.assertNotIn(hidden, " ".join(found))
                # The Work step announcements are never streamed themselves.
                self.assertNotIn('work_step": {', text)

    async def test_code_turn_shows_the_workspace_being_prepared(self) -> None:
        events = [
            {"work_step": {"phase": "preparing"}},
            project(1, "workspace", None, "completed"),
            {"work_step": {"tool": "bash"}},
            project(2, "tool", "bash", "running"),
            project(3, "tool", "bash", "completed"),
            project(4, "recap", None, "completed"),
        ]
        payload = self.payload("Lance les tests de mon script Python")
        for stable in (True, False):
            with (
                self.subTest(stable=stable),
                patch.object(work_progress, "MINIMUM_INTERVAL_SECONDS", 0.0),
            ):
                found, rows = await self.statuses(payload, events, stable)
                self.assertEqual(
                    found,
                    [
                        "Réflexion…",
                        "Préparation de l’espace de travail…",
                        "Exécution d’une commande…",
                        "Réflexion…",
                        "Réponse terminée",
                    ],
                )
                text = json.dumps(rows, ensure_ascii=False)
                self.assertNotIn('"phase": "preparing"', text)
                self.assertNotIn('work_step": {', text)

    async def test_a_declined_hand_over_leaves_no_preparing_status(self) -> None:
        events = [
            {"work_step": {"phase": "preparing"}},
            {"work_step": {"phase": "declined"}},
        ]
        with patch.object(work_progress, "MINIMUM_INTERVAL_SECONDS", 0.0):
            found, _ = await self.statuses(
                self.payload("Write a script and run it", "en"), events, True
            )
        self.assertEqual(found[-2:], ["Thinking…", "Answer complete"])

    async def test_english_ui_and_italian_conversation(self) -> None:
        events = [
            {"work_step": {"tool": "bash"}},
            project(1, "tool", "bash", "running"),
        ]
        with patch.object(work_progress, "MINIMUM_INTERVAL_SECONDS", 0.0):
            found, _ = await self.statuses(
                self.payload("Run the tests of my script", "en"), events, True
            )
            self.assertIn("Running a command…", found)
            found, _ = await self.statuses(
                self.payload("Esegui i test del mio script per favore", locale="it"),
                events,
                True,
            )
            self.assertIn("Esecuzione di un comando…", found)

    async def test_status_changes_are_throttled(self) -> None:
        payload = self.payload("Rédige une lettre de motivation au format docx")
        _, rows = await self.statuses(payload, self.DOCUMENT_TURN, True)
        relayed = [
            row
            for row in rows
            if (row.get("atlas") or {}).get("phase") == "work_progress"
        ]
        # All the events arrive within a second: one status, the rest waits for
        # the heartbeat.
        self.assertEqual(len(relayed), 1)

    async def test_project_interface_turn_is_unchanged(self) -> None:
        payload = self.payload(
            "Rédige une lettre de motivation au format docx",
            org_id="00000000-0000-4000-8000-000000000001",
            project_id="00000000-0000-4000-8000-000000000002",
            conversation_id="00000000-0000-4000-8000-000000000003",
        )
        with patch.object(work_progress, "MINIMUM_INTERVAL_SECONDS", 0.0):
            _, rows = await self.statuses(payload, self.DOCUMENT_TURN, True)
        self.assertFalse(
            any(
                (row.get("atlas") or {}).get("phase") == "work_progress" for row in rows
            )
        )


if __name__ == "__main__":
    unittest.main()
