"""New long-session verifier controls, independent of D01-D30 and any provider."""

import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from evals.document_tasks.build_long_sessions import build
from evals.document_tasks.long_sessions import ROOT, verify


class LongSessionTests(unittest.TestCase):
    def test_additive_task_contract(self) -> None:
        from services.retrieval.project_work_contract import schemas, validate

        fields = schemas()["EvaluationTask"]["properties"]
        for task in ("D31", "D32", "D33"):
            metadata = json.loads((ROOT / task / "task.json").read_text())
            validate(
                "EvaluationTask",
                {key: value for key, value in metadata.items() if key in fields},
            )

    def test_all_good_controls_and_corruptions(self) -> None:
        for task in ("D31", "D32", "D33"):
            private = ROOT / task / "private"
            controls = json.loads((private / "controls.json").read_text())
            for name in ["good", *controls["corruptions"]]:
                archive = private / (
                    "good.tar.gz"
                    if name == "good"
                    else "corruptions/" + name + ".tar.gz"
                )
                with (
                    self.subTest(task=task, control=name),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    with tarfile.open(archive) as tar:
                        tar.extractall(directory, filter="data")
                    failures = verify(task, Path(directory), controls=True)
                    self.assertEqual(not failures, name == "good", failures)
                    self.assertTrue(
                        verify(task, Path(directory)),
                        "Synthetic controls must fail recorded measurement",
                    )

    def test_deterministic_public_manifests_and_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            build(target)
            for path in ROOT.glob("D3[1-3]/**/*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    self.assertEqual(
                        path.read_bytes(),
                        (target / path.relative_to(ROOT)).read_bytes(),
                        str(path),
                    )
            for task in ("D31", "D32", "D33"):
                meta = json.loads((target / task / "task.json").read_text())
                archive = target / task / "base.tar.gz"
                self.assertEqual(
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                    meta["base_archive_sha256"],
                )
                with tarfile.open(archive) as tar:
                    self.assertEqual(
                        set(tar.getnames()),
                        {row["path"] for row in meta["input_manifest"]},
                    )
                    self.assertFalse(any("private" in name for name in tar.getnames()))
                    for row in meta["input_manifest"]:
                        content = tar.extractfile(row["path"])
                        assert content is not None
                        raw = content.read()
                        self.assertEqual(len(raw), row["size_bytes"])
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"])
