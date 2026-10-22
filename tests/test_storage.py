import subprocess
import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import patch
import time
import unittest
from typing import ClassVar

import psycopg
from pydantic import ValidationError

from services.retrieval.store import Chunk, Store, main


class StorageTests(unittest.TestCase):
    container: ClassVar[str]
    dsn: ClassVar[str]
    store: ClassVar[Store]

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-p",
                "127.0.0.1::5432",
                "pgvector/pgvector:pg16",
            ],
            text=True,
        ).strip()
        cls.addClassCleanup(
            subprocess.run,
            ["docker", "rm", "-f", cls.container],
            check=True,
            capture_output=True,
        )
        port = (
            subprocess.check_output(
                ["docker", "port", cls.container, "5432"], text=True
            )
            .strip()
            .rsplit(":", 1)[1]
        )
        cls.dsn = f"host=127.0.0.1 port={port} user=postgres dbname=postgres connect_timeout=2"
        deadline = time.monotonic() + 45
        while True:
            try:
                with psycopg.connect(cls.dsn):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        cls.store = Store(cls.dsn)
        cls.store.initialize()

    def setUp(self) -> None:
        self.store.sync([])
        self.chunk = Chunk(
            chunk_id="a" * 64,
            doc_id="doc",
            source="doc.md",
            langue="fr",
            source_sha256="b" * 64,
            embedding_revision="test-revision",
            position=0,
            text="preuve العربية 中文",
            embedding=[1.0, 0.5],
        )

    def test_roundtrip_idempotence_replacement_deletion(self) -> None:
        self.store.sync([self.chunk])
        self.store.sync([self.chunk])
        self.store.initialize()
        self.assertEqual(self.store.read(), [self.chunk])
        self.assertEqual(self.store.resolve(self.chunk.chunk_id), self.chunk)
        replacement = self.chunk.model_copy(
            update={"chunk_id": "c" * 64, "text": "changed"}
        )
        self.store.sync([replacement])
        with self.assertRaises(KeyError):
            self.store.resolve(self.chunk.chunk_id)
        self.assertEqual(self.store.read(), [replacement])
        self.store.sync([])
        self.assertEqual(self.store.read(), [])

    def test_transaction_rolls_back_on_duplicate(self) -> None:
        self.store.sync([self.chunk])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.store.sync([self.chunk, self.chunk])
        self.assertEqual(self.store.read(), [self.chunk])

    def test_rejects_inconsistent_or_invalid_vectors_before_write(self) -> None:
        self.store.sync([self.chunk])
        invalid: list[dict[str, object]] = [
            {"embedding": [float("nan")]},
            {"embedding": [0.0, 0.0]},
            {"embedding": []},
            {"embedding": [1.0] * 1025},
            {"text": " "},
            {"position": -1},
            {"chunk_id": "invalid"},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.store.sync([self.chunk.model_copy(update=values)])
        inconsistent: list[dict[str, object]] = [
            {"embedding_revision": "different"},
            {"embedding": [1.0]},
            {"source": "other.md"},
            {"langue": "es"},
            {"source_sha256": "c" * 64},
        ]
        for values in inconsistent:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.store.sync([self.chunk, self.chunk.model_copy(update=values)])
        self.assertEqual(self.store.read(), [self.chunk])
        with self.assertRaises(KeyError):
            self.store.resolve("' OR 1=1 --")

    def test_migration_reversible_without_dropping_extension(self) -> None:
        self.store.sync([self.chunk])
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DROP SCHEMA atlas_retrieval CASCADE")
            self.assertIsNotNone(
                conn.execute(
                    "SELECT extname FROM pg_extension WHERE extname='vector'"
                ).fetchone()
            )
        self.store.initialize()
        self.assertEqual(self.store.read(), [])
        self.store.sync([self.chunk])
        self.assertEqual(self.store.read(), [self.chunk])

    def test_cli_init_sync_read_and_bad_input(self) -> None:
        with patch.dict(os.environ, {"ATLAS_RETRIEVAL_DSN": self.dsn}):
            for action in ["init", "sync", "read"]:
                output = io.StringIO()
                with (
                    patch("sys.argv", ["store", action]),
                    patch(
                        "sys.stdin", io.StringIO(json.dumps([self.chunk.model_dump()]))
                    ),
                    redirect_stdout(output),
                ):
                    main()
                if action == "read":
                    self.assertEqual(
                        json.loads(output.getvalue())[0]["text"], self.chunk.text
                    )
            with (
                patch("sys.argv", ["store", "sync"]),
                patch("sys.stdin", io.StringIO("{}")),
                self.assertRaisesRegex(ValueError, "expected_chunk_list"),
            ):
                main()
