import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError
from services.orchestrator.artifact_tools import (
    ArtifactToolRuntime,
    clean_summary,
    compose_answer,
    current_artifact_message,
    declarations,
    footer,
    parse_history_reference,
    unchanged_footer,
)
from services.orchestrator.loop import Call

Message = dict[str, object]


HTML = "<html><body><button>Run</button></body></html>"
UPDATED_HTML = "<html><body><button>Changed</button></body></html>"


class ArtifactToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"ATLAS_DATA_DIR": self.directory.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_declaration_schema(self) -> None:
        declaration = declarations()[0]["function"]
        assert isinstance(declaration, dict)
        self.assertEqual(declaration["name"], "create_artifact")
        properties = declaration["parameters"]["properties"]
        self.assertEqual(properties["title"]["maxLength"], 80)
        self.assertEqual(
            properties["filename"]["pattern"], r"^[a-z0-9][a-z0-9-]{0,60}\.html$"
        )
        self.assertEqual(properties["summary"]["maxLength"], 600)
        self.assertEqual(
            properties["artifact_id"]["anyOf"][0]["pattern"], r"^[0-9a-f]{32}$"
        )

    def test_execute_creates_and_updates(self) -> None:
        runtime = ArtifactToolRuntime("user@example.test", "request-1")
        first = asyncio.run(runtime.execute(call(HTML), 1))
        assert isinstance(first, dict)
        artifact_id = str(first["artifact_id"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["bytes"], len(HTML.encode()))
        second = asyncio.run(runtime.execute(call(UPDATED_HTML, artifact_id), 1))
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            artifact_store.read(artifact_id, "user@example.test", 2).content,
            UPDATED_HTML.encode(),
        )

    def test_execute_reports_validation_and_rejects_after_two_failures(self) -> None:
        runtime = ArtifactToolRuntime("user@example.test", "request-1")
        first = asyncio.run(runtime.execute(call("<p>invalid</p>"), 1))
        assert isinstance(first, dict)
        problems = first["problems"]
        assert isinstance(problems, list)
        self.assertEqual(first["error"], "artifact_invalid")
        self.assertEqual(
            problems[0],
            {"code": "not_a_document", "line": 1, "detail": "<p>invalid</p>"},
        )
        self.assertTrue(runtime.forced is False)
        second = asyncio.run(runtime.execute(call("<p>invalid</p>"), 1))
        self.assertEqual(second["error"], "artifact_rejected")
        self.assertFalse(runtime.offered)

    def test_execute_maps_store_errors_and_respects_current_id(self) -> None:
        runtime = ArtifactToolRuntime("user@example.test", "request-1")
        unknown = asyncio.run(runtime.execute(call(HTML, "0" * 32), 1))
        self.assertEqual(unknown["error"], "artifact_unknown")
        with patch.object(
            artifact_store,
            "create",
            side_effect=ArtifactError("artifact_store_unavailable"),
        ):
            runtime = ArtifactToolRuntime("user@example.test", "request-1")
            unavailable = asyncio.run(runtime.execute(call(HTML), 1))
        self.assertEqual(unavailable["error"], "artifact_unavailable")

    def test_history_parser_and_owner_check(self) -> None:
        created = artifact_store.create(
            "user@example.test", "Snake", "snake.html", HTML.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        messages: list[Message] = [
            {
                "role": "assistant",
                "content": f"[Open Snake](/api/atlas/artifacts/{artifact_id}/versions/1/open)",
            },
            {"role": "user", "content": "change it"},
        ]
        reference = parse_history_reference(messages)
        assert reference is not None
        self.assertEqual(reference.title, "Open Snake")
        current = current_artifact_message(messages, "user@example.test")
        assert current is not None
        self.assertEqual(
            current.message,
            {
                "role": "system",
                "content": (
                    "Current app (update it with create_artifact and its "
                    "artifact_id): "
                    + json.dumps(
                        {
                            "current_artifact": {
                                "artifact_id": artifact_id,
                                "version": 1,
                                "title": "Open Snake",
                                "filename": "snake.html",
                                "content": HTML,
                            }
                        },
                        ensure_ascii=False,
                    )
                ),
            },
        )
        self.assertEqual(
            current.artifact,
            {
                "artifact_id": artifact_id,
                "version": 1,
                "title": "Open Snake",
                "filename": "snake.html",
                "content": HTML,
            },
        )
        missing = current_artifact_message(messages, "other@example.test")
        assert missing is not None
        self.assertEqual(
            missing.message,
            {
                "role": "system",
                "content": json.dumps(
                    {
                        "current_artifact": None,
                        "note": "previous app no longer stored (30-day retention)",
                    },
                    ensure_ascii=False,
                ),
            },
        )
        self.assertIsNone(missing.artifact)

    def test_current_artifact_omits_oversized_source(self) -> None:
        large = "<html><body>" + ("x" * 100000) + "</body></html>"
        created = artifact_store.create(
            "user@example.test", "Large", "large.html", large.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        messages: list[Message] = [
            {
                "role": "assistant",
                "content": f"/api/atlas/artifacts/{artifact_id}/versions/1/open",
            },
            {"role": "user", "content": "change it"},
        ]
        current = current_artifact_message(messages, "user@example.test")
        assert current is not None
        artifact = current.artifact
        assert isinstance(artifact, dict)
        self.assertNotIn("content", artifact)
        self.assertEqual(artifact["note"], "source too large to include")

    def test_summary_cleaning(self) -> None:
        self.assertEqual(
            clean_summary("![alt](image.png) <b>Use</b> the app"),
            "Use the app",
        )

    def test_localized_footer_and_answer(self) -> None:
        text = footer("0" * 32, 1, "Snake [game]", "snake.html", "fr")
        self.assertIn("Ouvrir « Snake \\[game\\] » (version 1)", text)
        self.assertIn("Télécharger snake.html", text)
        self.assertIn("Aperçu temporaire", text)
        self.assertIn("title=Snake%20%5Bgame%5D", text)
        self.assertNotIn(
            "Aperçu temporaire", footer("0" * 32, 2, "Snake", "snake.html", "fr")
        )
        self.assertEqual(
            unchanged_footer(2, "en"),
            "The change could not be applied; version 2 is unchanged.",
        )
        answer = compose_answer(
            "Here it is.",
            "![alt](image.png) <b>Play</b>.",
            "0" * 32,
            1,
            "Snake",
            "snake.html",
            "en",
        )
        self.assertIn("Here it is.\n\nPlay.\n\n[Open", answer)


def call(content: str, artifact_id: str | None = None) -> Call:
    arguments = {
        "title": "Snake",
        "filename": "snake.html",
        "content": content,
        "summary": "A simple game.",
    }
    if artifact_id is not None:
        arguments["artifact_id"] = artifact_id
    return Call("call-1", "create_artifact", repr(arguments).replace("'", '"'))
