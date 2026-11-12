import json
import os
import tempfile
import unittest
from unittest.mock import patch

from services.orchestrator import artifact_store
from services.orchestrator.artifact_tools import (
    clean_summary,
    compose_answer,
    current_artifact_message,
    footer,
    parse_history_reference,
    unchanged_footer,
)

Message = dict[str, object]


HTML = "<html><body><button>Run</button></body></html>"


class ArtifactToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"ATLAS_DATA_DIR": self.directory.name})
        self.env.start()
        self.addCleanup(self.env.stop)

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
                    "Current app of this conversation (context only; Atlas applies "
                    "changes to it): "
                    + json.dumps(
                        {
                            "current_artifact": {
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

    def test_footer_title_survives_ampersands_and_is_read_back(self) -> None:
        text = footer("a" * 32, 3, "Tom & Jerry", "tom.html", "en")
        self.assertIn("title=Tom%20%26%20Jerry&artifact=", text)
        reference = parse_history_reference(
            [{"role": "assistant", "content": text}, {"role": "user", "content": "x"}]
        )
        assert reference is not None
        self.assertEqual(
            (reference.artifact_id, reference.version, reference.title),
            ("a" * 32, 3, "Tom & Jerry"),
        )

    def test_side_view_chip_keeps_the_language_of_the_footer(self) -> None:
        # The chip replaces the open link of the footer; the download link next
        # to it keeps the server wording, so both must read in one language.
        import re
        from pathlib import Path

        from services.orchestrator.artifact_tools import FOOTER_LABELS
        from services.orchestrator.side_view import OPEN_LABELS

        script = Path("services/orchestrator/chat-ui.js").read_text()
        table = re.search(r"const answerOpeners = \{(.*)\};", script)
        assert table is not None
        openers = dict(re.findall(r"(\w\w): /(.+?)/(?=,| )", table[1]))
        self.assertEqual(set(openers), set(FOOTER_LABELS))
        self.assertEqual(set(openers), set(OPEN_LABELS))
        for language, labels in FOOTER_LABELS.items():
            written = [
                labels["open"].format(title="Le Pain d'Élise", version=12),
                OPEN_LABELS[language],
            ]
            for label in written:
                matching = [
                    key for key, pattern in openers.items() if re.search(pattern, label)
                ]
                self.assertEqual(matching, [language], label)
        self.assertIn("node.textContent = openText(node.textContent);", script)

    def test_side_view_shows_a_produced_version_by_itself(self) -> None:
        # Focusing the frame on load marked every app as used, so a new version
        # always waited behind a Reload bar (staging 2026-11-12).
        from pathlib import Path

        script = Path("services/orchestrator/chat-ui.js").read_text()
        self.assertNotIn("interacted", script)
        self.assertIn("produced = latest && descriptor.version > known;", script)
        self.assertIn("if (produced) open(descriptor)", script)

    def test_drawing_footer_has_no_data_notice(self) -> None:
        text = footer("0" * 32, 1, "Cat", "cat.html", "en", temporary=False)
        self.assertNotIn("Temporary preview", text)
