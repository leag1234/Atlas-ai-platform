"""M7 logs are append-only, private and scrub secrets on every field."""

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.orchestrator.interactions import Interaction, write_interaction


class InteractionTests(unittest.TestCase):
    def test_log_fields_redaction_and_permissions(self) -> None:
        secret = "test-only-injected-credential"
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"SCW_GENERATIVE_API_KEY": secret}),
        ):
            directory = Path(root) / "interactions"
            item = Interaction(
                question="question " + secret, reponse="response " + secret
            )
            item.citations = [{"text": secret}]
            write_interaction(item, directory)
            write_interaction(item, directory)
            path = next(directory.glob("*.jsonl"))
            text = path.read_text()
            self.assertNotIn(secret, text)
            self.assertIn("[REDACTED]", text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(len(rows), 2)
            required = set(
                (
                    "timestamp question reponse modele_utilise route_decision latence_ms "
                    "chunks_recuperes citations tokens cout_eur erreurs state request_id"
                ).split()
            )
            self.assertTrue(required <= rows[0].keys())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_log_failure_is_not_silenced(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "file"
            path.write_text("occupied")
            with self.assertRaises(OSError):
                write_interaction(Interaction(), path)

    def test_concurrent_rows_and_symlink_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "logs"
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(
                    pool.map(
                        lambda i: write_interaction(
                            Interaction(question=str(i)), directory
                        ),
                        range(20),
                    )
                )
            path = next(directory.glob("*.jsonl"))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 20)
            target = Path(root) / "protected"
            target.write_text("unchanged")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(OSError):
                write_interaction(Interaction(), directory)
            self.assertEqual(target.read_text(), "unchanged")
