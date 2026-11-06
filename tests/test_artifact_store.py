import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError, create, read, update


class ArtifactStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "ATLAS_DATA_DIR": self.directory.name,
                "ATLAS_ARTIFACT_OWNER": "user@example.test",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_create_read_and_immutable_versions(self) -> None:
        first = create(
            " User@Example.test ", " Report ", "report.html", b"first", "request-1"
        )
        second = update(
            str(first["id"]),
            "user@example.test",
            "Report",
            "report.html",
            b"second",
            "request-2",
        )
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            read(str(first["id"]), "user@example.test", 1).content, b"first"
        )
        self.assertEqual(read(str(first["id"]), "user@example.test").content, b"second")
        with self.assertRaises(ArtifactError) as raised:
            update(
                str(first["id"]),
                "user@example.test",
                "Report",
                "report.html",
                b"third",
                "request-3",
                base_version=1,
            )
        self.assertEqual(raised.exception.code, "artifact_conflict")

    def test_owner_and_integrity_checks(self) -> None:
        result = create(
            "user@example.test", "Report", "report.html", b"secret", "request-1"
        )
        with self.assertRaises(ArtifactError) as raised:
            read(str(result["id"]), "other@example.test")
        self.assertEqual(raised.exception.code, "artifact_not_found")
        path = (
            Path(self.directory.name)
            / "artifacts"
            / "blobs"
            / str(result["id"])
            / "1.html"
        )
        path.write_bytes(b"changed")
        with self.assertRaises(ArtifactError) as raised:
            read(str(result["id"]), "user@example.test")
        self.assertEqual(raised.exception.code, "artifact_unavailable")

    def test_quotas(self) -> None:
        result = create(
            "user@example.test", "Report", "report.html", b"first", "request-1"
        )
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_MAX_VERSIONS": "1"}):
            with self.assertRaises(ArtifactError) as raised:
                update(
                    str(result["id"]),
                    "user@example.test",
                    "Report",
                    "report.html",
                    b"second",
                    "request-2",
                )
        self.assertEqual(raised.exception.code, "artifact_quota")

    def test_retention_and_purge_counts_only(self) -> None:
        result = create(
            "user@example.test", "Report", "report.html", b"first", "request-1"
        )
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_RETENTION_DAYS": "0"}):
            with self.assertRaises(ArtifactError) as raised:
                read(str(result["id"]), "user@example.test")
            self.assertEqual(raised.exception.code, "artifact_not_found")
            counts = artifact_store.purge(dry_run=True)
            self.assertEqual(counts, {"versions": 1, "artifacts": 0})
            counts = artifact_store.purge()
            self.assertEqual(counts, {"versions": 1, "artifacts": 1})
        with self.assertRaises(ArtifactError) as raised:
            read(str(result["id"]), "user@example.test")
        self.assertEqual(raised.exception.code, "artifact_not_found")

    def test_cli_seed_and_purge(self) -> None:
        source = Path(self.directory.name) / "input.html"
        source.write_bytes(b"<p>probe</p>")
        argv = ["seed", str(source)]
        with patch("sys.argv", ["artifact_store.py", *argv]):
            with patch("builtins.print") as printed:
                self.assertEqual(artifact_store.main(), 0)
        printed.assert_called_once_with(
            artifact_store.read(
                printed.call_args.args[0], "user@example.test"
            ).artifact_id,
            1,
        )
        with patch("sys.argv", ["artifact_store.py", "purge", "--dry-run"]):
            with patch("builtins.print") as printed:
                self.assertEqual(artifact_store.main(), 0)
        printed.assert_called_once_with("versions=0 artifacts=0")


if __name__ == "__main__":
    unittest.main()
