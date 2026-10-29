"""REQ-PROJ-09 verifier qualification only: never an evaluated agent/model run."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from evals.document_tasks.build_fixtures import build
from evals.document_tasks.catalog import LANGUAGES, TASK_IDS, TITLES
from evals.document_tasks.formats import digest, pack, unpack
from evals.document_tasks.verifier import verify
from services.retrieval.project_work_contract import matches, schemas

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "evals/document_tasks"


class DocumentTaskTests(unittest.TestCase):
    def temporary(self) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory(
            prefix="document-tests-", dir=ROOT / "BRAIN"
        )
        self.addCleanup(directory.cleanup)
        return directory

    def test_all_metadata_and_language_balance(self) -> None:
        directories = [TASKS / identifier / "task.json" for identifier in TASK_IDS]
        self.assertEqual([p.parent.name for p in directories], list(TASK_IDS))
        languages: Counter[str] = Counter()
        for number, path in enumerate(directories, 1):
            with self.subTest(task=path.parent.name):
                data = json.loads(path.read_text())
                languages[data["language"]] += 1
                self.assertEqual(data["title"], TITLES[number - 1])
                self.assertTrue(data["prompt"])
                self.assertEqual(len(data["variants"]), 6)
                self.assertTrue(
                    any(
                        v["kind"] == "negative" and not v["execute"]
                        for v in data["variants"]
                    )
                )
                self.assertLessEqual(
                    len(
                        next(
                            v["prompt"]
                            for v in data["variants"]
                            if v["kind"] == "short"
                        ).split()
                    ),
                    4,
                )
                contract = {
                    k: v
                    for k, v in data.items()
                    if k not in {"title", "prompt", "variants"}
                }
                self.assertTrue(matches(contract, schemas()["EvaluationTask"]))
                archive = (path.parent / "base.tar.gz").read_bytes()
                self.assertEqual(digest(archive), data["base_archive_sha256"])
                with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                    self.assertGreaterEqual(len(source.getmembers()), 2)
                    self.assertEqual(
                        sorted(source.getnames()),
                        sorted(f["path"] for f in data["input_manifest"]),
                    )
                    for entry in data["input_manifest"]:
                        stream = source.extractfile(entry["path"])
                        self.assertIsNotNone(stream)
                        assert stream is not None
                        raw = stream.read()
                        self.assertEqual(
                            (len(raw), digest(raw)),
                            (entry["size_bytes"], entry["sha256"]),
                        )
                    self.assertFalse(
                        any(
                            "private/" in n or "oracle" in n or "verifier" in n
                            for n in source.getnames()
                        )
                    )
        self.assertEqual(languages, Counter(dict.fromkeys(LANGUAGES, 6)))

    def test_every_known_good_output_passes_and_every_corruption_fails(self) -> None:
        for task in (TASKS / identifier for identifier in TASK_IDS):
            with self.subTest(task=task.name, control="known-good"):
                destination = Path(self.temporary().name)
                unpack((task / "private/good.tar.gz").read_bytes(), destination)
                verify(task, destination, controls=True)
            controls = json.loads((task / "private/controls.json").read_text())
            paths = sorted((task / "private/corruptions").glob("*.tar.gz"))
            self.assertEqual(
                [p.name.removesuffix(".tar.gz") for p in paths], controls["corruptions"]
            )
            self.assertGreaterEqual(len(paths), 3)
            for corruption in paths:
                with self.subTest(task=task.name, corruption=corruption.name):
                    destination = Path(self.temporary().name)
                    unpack(corruption.read_bytes(), destination)
                    with self.assertRaises(
                        (ValueError, OSError, KeyError, TypeError, IndexError)
                    ):
                        verify(task, destination, controls=True)

    def test_rebuilding_twice_matches_every_committed_fixture_hash(self) -> None:
        first, second = Path(self.temporary().name), Path(self.temporary().name)
        build(first)
        build(second)

        def inventory(root: Path) -> dict[str, str]:
            return {
                p.relative_to(root).as_posix(): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for identifier in TASK_IDS
                for p in (root / identifier).glob("**/*")
                if p.is_file() and "__pycache__" not in p.parts
            }

        self.assertEqual(inventory(first), inventory(second))
        self.assertEqual(inventory(first), inventory(TASKS))

    def test_fixture_controls_cannot_be_misrepresented_as_recordings(self) -> None:
        task = TASKS / "D01"
        destination = Path(self.temporary().name)
        unpack((task / "private/good.tar.gz").read_bytes(), destination)
        with self.assertRaisesRegex(ValueError, "untrusted_observations"):
            verify(task, destination)

    def test_outer_archive_rejects_escape_duplicates_links_and_bombs(self) -> None:
        destination = Path(self.temporary().name)
        for kind in ("escape", "duplicate", "link", "oversized"):
            with self.subTest(kind=kind):
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode="w:gz") as archive:
                    for _ in range(2 if kind == "duplicate" else 1):
                        member = tarfile.TarInfo(
                            "../escape" if kind == "escape" else "entry"
                        )
                        member.size = 2
                        if kind == "link":
                            member.type, member.linkname, member.size = (
                                tarfile.SYMTYPE,
                                "../outside",
                                0,
                            )
                        archive.addfile(member, io.BytesIO(b"xx"))
                with self.assertRaises(ValueError):
                    unpack(
                        output.getvalue(),
                        destination,
                        maximum=1 if kind == "oversized" else 100,
                    )
        unpack(pack({"safe/input.txt": b"data"}), destination)
        self.assertEqual((destination / "safe/input.txt").read_bytes(), b"data")

    def test_scans_are_images_long_pdf_has_full_coverage_and_bomb_is_bounded(
        self,
    ) -> None:
        from pypdf import PdfReader
        from zipfile import ZipFile

        for name in ("D02", "D03", "D26"):
            destination = Path(self.temporary().name)
            unpack((TASKS / name / "base.tar.gz").read_bytes(), destination)
            if name == "D02":
                for file in destination.glob("*.pdf"):
                    reader = PdfReader(file)
                    self.assertEqual(len(reader.pages), 2)
                    self.assertTrue(
                        all(
                            not p.extract_text().strip() and p.images
                            for p in reader.pages
                        )
                    )
            if name == "D03":
                reader = PdfReader(destination / "long.pdf")
                self.assertEqual(len(reader.pages), 60)
                for index, term in ((0, "Approval"), (29, "Midpoint"), (59, "Closure")):
                    self.assertIn(term, reader.pages[index].extract_text())
            if name == "D26":
                with ZipFile(destination / "bomb.zip") as archive:
                    self.assertEqual(
                        sum(i.file_size for i in archive.infolist()), 2097152
                    )
                    self.assertLess((destination / "bomb.zip").stat().st_size, 10000)


class DocumentHarnessContractTests(unittest.TestCase):
    """Pure accounting/transport-contract tests, not recorded or live task runs."""

    def test_denial_status_cannot_hide_foreign_bytes_in_error_body(self) -> None:
        from evals.document_tasks.work_adapter import denial_leaked_bytes
        from services.retrieval.project_identity import ProjectError

        body = ProjectError(404, "not_found").body
        raw = json.dumps(body).encode()
        self.assertEqual(denial_leaked_bytes(404, raw), 0)
        body["message"] = "FOREIGN-CEDAR-CANARY"
        raw = json.dumps(body).encode()
        self.assertEqual(denial_leaked_bytes(404, raw), len(raw))
        self.assertEqual(denial_leaked_bytes(404, b"foreign document bytes"), 22)

    def test_owner_freeze_is_required_before_any_runner_can_launch(self) -> None:
        from evals.document_tasks.run import load_tasks, require_freeze, run_task
        from unittest.mock import patch

        tasks = load_tasks()
        with patch("evals.document_tasks.run.subprocess.run") as launch:
            with self.assertRaisesRegex(ValueError, "owner_review"):
                run_task(
                    tasks[0],
                    ["must-never-run"],
                    mode="recorded",
                    approval=TASKS / "review.example.json",
                )
            launch.assert_not_called()
        with self.assertRaises(ValueError):
            require_freeze(TASKS / "review.example.json", "live")

    def test_http_work_request_withholds_private_keys_and_observer_credentials(
        self,
    ) -> None:
        from evals.document_tasks.work_adapter import ProjectWorkRunner
        from evals.document_tasks.run import load_tasks

        binding = dict(
            org_id="10000000-0000-4000-8000-000000000001",
            project_id="10000000-0000-4000-8000-000000000002",
            conversation_id="10000000-0000-4000-8000-000000000003",
            session_id="10000000-0000-4000-8000-000000000004",
            profile="public-profile",
            inputs={},
            foreign=dict(
                file_id="10000000-0000-4000-8000-000000000005",
                owner_token_env="OWNER_CREDENTIAL_REFERENCE",
            ),
        )
        runner = ProjectWorkRunner(
            "http://127.0.0.1:18020",
            "synthetic-test-credential",
            binding,
            ["private-observer-command"],
        )
        request = runner.work_request(load_tasks()[26].public)
        self.assertEqual(request["project_action"], "work")
        self.assertEqual(request["work_session_id"], binding["session_id"])
        payload = json.dumps(request)
        for forbidden in (
            "OWNER_CREDENTIAL_REFERENCE",
            "synthetic-test-credential",
            "private-observer-command",
            "expectations.json",
            "good.tar.gz",
            "verifier_ref",
        ):
            self.assertNotIn(forbidden, payload)
        for port in (8000, 8020, 3000, 5432):
            with self.assertRaises(ValueError):
                ProjectWorkRunner(f"http://127.0.0.1:{port}", "", binding, [])

    def test_replay_deduplicates_and_rejects_changed_event_identity(self) -> None:
        from evals.document_tasks.work_adapter import ProjectWorkRunner

        binding = dict(
            org_id="10000000-0000-4000-8000-000000000001",
            project_id="10000000-0000-4000-8000-000000000002",
            conversation_id="10000000-0000-4000-8000-000000000003",
            session_id="10000000-0000-4000-8000-000000000004",
        )
        runner = ProjectWorkRunner("http://127.0.0.1:18020", "", binding, [])
        raw = 'data: {"seq": 3, "event_id": "event-three"}\n\ndata: {"seq": 4, "type": "tool"}\n'
        runner.events(raw)
        runner.events(raw)
        self.assertEqual(runner.sequences, {"event-three": 3, "progress:4": 4})
        with self.assertRaises(ValueError):
            runner.events('data: {"seq": 8, "event_id": "event-three"}\n')

    def test_both_acceptance_levels_and_every_mandatory_gate(self) -> None:
        import copy
        from typing import Any
        from evals.document_tasks.run import aggregate, load_tasks

        cases: list[dict[str, Any]] = [
            dict(
                id=t.identifier,
                language=t.public["language"],
                status="passed",
                attempt=1,
                artifact_hashes=[],
                verifier_exit=0,
                bytes=0,
                latency_seconds=1,
                tool_calls=1,
                model_calls=1,
                cost_eur=0.01,
                ledger_reconciled=True,
            )
            for t in load_tasks()
        ]
        evidence = [
            dict(
                id=c["id"],
                false_success_claim=False,
                cross_org_leaks=0,
                local_only_leaks=0,
                budgets_respected=True,
            )
            for c in cases
        ]
        config = json.loads((TASKS / "pass_conditions.json").read_text())
        independent = {
            key: dict(
                status="passed",
                evidence_sha256="a" * 64,
                ci_run="https://github.com/example/project/actions/runs/123",
            )
            for key in config["independent_integration_gates"]
        }
        all_green = aggregate(cases, evidence, mode="recorded", independent=independent)
        self.assertTrue(all_green["ga_target_met"])
        self.assertTrue(all_green["preview_passed"])
        self.assertEqual(all_green["mode"], "offline")
        preview = copy.deepcopy(cases)
        for case in preview[:15]:
            case["status"] = "failed"
        report = aggregate(preview, evidence, mode="recorded", independent=independent)
        self.assertTrue(report["preview_passed"])
        self.assertFalse(report["ga_target_met"])
        for change in (
            "safety",
            "false_claim",
            "cross_org",
            "local_only",
            "budget",
            "ledger",
            "integration",
            "language",
        ):
            with self.subTest(change=change):
                changed_cases, changed_evidence, gates = (
                    copy.deepcopy(cases),
                    copy.deepcopy(evidence),
                    copy.deepcopy(independent),
                )
                if change == "safety":
                    changed_cases[24]["status"] = "failed"
                elif change == "false_claim":
                    changed_evidence[0]["false_success_claim"] = True
                elif change == "cross_org":
                    changed_evidence[0]["cross_org_leaks"] = 1
                elif change == "local_only":
                    changed_evidence[0]["local_only_leaks"] = 1
                elif change == "budget":
                    changed_evidence[0]["budgets_respected"] = False
                elif change == "ledger":
                    changed_cases[0]["ledger_reconciled"] = False
                elif change == "integration":
                    gates.pop("rls")
                else:
                    changed_cases[0]["status"] = changed_cases[5]["status"] = "failed"
                report = aggregate(
                    changed_cases, changed_evidence, mode="recorded", independent=gates
                )
                self.assertFalse(report["ga_target_met"])
                self.assertEqual(report["preview_passed"], change == "language")
        with self.assertRaises(ValueError):
            aggregate(
                cases[:-1], evidence[:-1], mode="recorded", independent=independent
            )
        duplicate = copy.deepcopy(cases)
        duplicate[-1] = duplicate[0]
        with self.assertRaises(ValueError):
            aggregate(duplicate, evidence, mode="recorded", independent=independent)

    def test_ledger_reconciliation_detects_bytes_cost_replay_and_tampering(
        self,
    ) -> None:
        import copy
        from typing import Any
        from evals.document_tasks.run import reconcile

        def signed(event: dict[str, Any]) -> dict[str, Any]:
            event["digest"] = digest(
                json.dumps(
                    {k: v for k, v in event.items() if k != "digest"},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            )
            return event

        dispatch = dict(
            call_id="call-one",
            attempt=1,
            byte_counts=dict(serialized_payload_bytes=120, response_bytes=20),
            transmission="acknowledged",
        )
        first = signed(
            dict(
                seq=1,
                event_id="event-one",
                previous_digest=None,
                kind="tool_started",
                session_id="session",
                model_dispatch=None,
            )
        )
        second = signed(
            dict(
                seq=2,
                event_id="event-two",
                previous_digest=first["digest"],
                kind="model_acknowledged",
                session_id="session",
                model_dispatch=dispatch,
            )
        )
        capture: dict[str, Any] = dict(
            session_id="session",
            ledger=[first, second],
            usage=dict(
                tool_calls=1,
                model_calls=1,
                elapsed_seconds=2,
                cost_eur=0.02,
                reserved_eur=0.01,
                unknown_eur=0,
            ),
            calls=[
                dict(
                    call_id="call-one",
                    attempt=1,
                    cost_eur=0.02,
                    reserved_eur=0.01,
                    unknown_eur=0,
                    serialized_payload_bytes=120,
                    response_bytes=20,
                    source_ranges=[
                        dict(version_id="source-one", locator="page=1", bytes=40)
                    ],
                )
            ],
            ledger_summary=dict(
                model=dict(calls=1, payload_bytes=120, unique_source_bytes=40),
                produced=dict(files=1, bytes=12),
            ),
        )
        artifacts = [dict(path="test.pdf", bytes=12)]
        result = reconcile(capture, artifacts)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["retained_reservations_eur"], 0.01)
        for kind in (
            "hash",
            "duplicate",
            "bytes",
            "cost",
            "produced",
            "unknown",
            "missing_call",
        ):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(capture)
                if kind == "hash":
                    changed["ledger"][0]["digest"] = "0" * 64
                elif kind == "duplicate":
                    changed["ledger"].append(changed["ledger"][1])
                elif kind == "bytes":
                    changed["calls"][0]["serialized_payload_bytes"] = 999
                elif kind == "cost":
                    changed["calls"][0]["cost_eur"] = 0.01
                elif kind == "produced":
                    changed["ledger_summary"]["produced"]["bytes"] = 0
                elif kind == "unknown":
                    changed["usage"]["reserved_eur"] = 0
                else:
                    changed["calls"] = []
                self.assertFalse(reconcile(changed, artifacts)["reconciled"])
        invalid = copy.deepcopy(capture)
        invalid["usage"]["cost_eur"] = float("nan")
        with self.assertRaises(ValueError):
            reconcile(invalid, artifacts)

    def test_multilingual_success_claims_and_explicit_noncompletion(self) -> None:
        from evals.document_tasks.run import claims_success

        for claim in (
            "The operation succeeded",
            "Created the file",
            "Document créé",
            "Erfolgreich erstellt",
            "Documento creado",
            "Documento completato",
        ):
            self.assertTrue(claims_success(claim))
        for claim in (
            "Not completed",
            "Cancelled before publication",
            "Unable to read the encrypted input",
        ):
            self.assertFalse(claims_success(claim))


if __name__ == "__main__":
    unittest.main()
