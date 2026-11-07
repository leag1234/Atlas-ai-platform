"""Offline tests for the artefact continuation benchmark."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from evals.artifacts import report, run


ARTIFACT_ID = "0123456789abcdef0123456789abcdef"
OPEN_PATH = f"/api/atlas/artifacts/{ARTIFACT_ID}/versions/1/open"
DOWNLOAD_PATH = f"/api/atlas/artifacts/{ARTIFACT_ID}/versions/1/download"


def chat_response(answer: str, cost: float = 0.01) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "atlas": {
            "cost_eur": cost,
            "reasoning": {"trace_tokens": 2},
            "artifact_validation_rejections": 1,
            "artifact_retries": 1,
            "artifact_tool_call_malformations": 1,
        },
    }


class FakeTransport:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.payloads: list[dict[str, Any]] = []

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.answers.pop(0)

    def get_bytes(self, path: str) -> bytes:
        return b"<html>stored</html>"


class FakeBrowser:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.exported = False

    def open(self, url: str) -> None:
        self.opened.append(url)

    def screenshot(self, path: str, theme: str, phone: bool) -> None:
        Path(path).write_bytes(b"png")

    def interact(self, kind: str) -> bool:
        return True

    def check_version(self, pattern: str) -> bool:
        return True

    def export(self, path: str) -> bool:
        self.exported = True
        Path(path).write_bytes(b"<html>stored</html>")
        return True

    def close(self) -> None:
        return None


class ArtifactBenchmarkTests(unittest.TestCase):
    def test_extracts_artifact_and_download_links(self) -> None:
        answer = f"Ready. [Open]({OPEN_PATH}) [Download]({DOWNLOAD_PATH})"
        artifact = run.extract_artifact(answer)
        self.assertEqual(artifact, (ARTIFACT_ID, 1, OPEN_PATH))
        self.assertEqual(run.extract_download(answer), DOWNLOAD_PATH)

    def test_parses_usage_and_counts_events(self) -> None:
        response = chat_response("answer")
        usage = run.parse_usage(response)
        self.assertEqual(
            (
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.reasoning_tokens,
                usage.cost_eur,
            ),
            (10, 20, 2, 0.01),
        )
        self.assertEqual(run.count_event(response, "validation_rejections"), 1)
        self.assertEqual(run.count_event(response, "retries"), 1)
        self.assertEqual(run.count_event(response, "tool_call_malformations"), 1)
        self.assertEqual(run.count_event(response, "truncations"), 0)

    def test_question_without_artifact_is_detected(self) -> None:
        self.assertTrue(run.is_question_instead_of_artifact("Which theme?", False))
        self.assertFalse(run.is_question_instead_of_artifact("Which theme?", True))

    def test_run_case_sequences_five_steps(self) -> None:
        create = chat_response(
            f"App ready [Open]({OPEN_PATH}) [Download]({DOWNLOAD_PATH})"
        )
        change = chat_response(
            f"Updated [Open](/api/atlas/artifacts/{ARTIFACT_ID}/versions/2/open) "
            f"[Download](/api/atlas/artifacts/{ARTIFACT_ID}/versions/2/download)"
        )
        transport = FakeTransport([create, change])
        browser = FakeBrowser()
        with tempfile.TemporaryDirectory() as directory:
            result = run.run_case(
                {
                    "id": "B2",
                    "language": "en",
                    "create": "Build it.",
                    "change": "Add it.",
                    "interaction": "timer",
                    "version_pattern": "long break",
                },
                transport,
                browser,
                "atlas-test",
                1,
                Path(directory),
                language_detector=lambda _answer: "en",
            )
        self.assertTrue(result.success, result.steps)
        self.assertEqual([step.name for step in result.steps], list(run.STEP_NAMES))
        self.assertEqual(browser.opened[0], OPEN_PATH)
        self.assertTrue(browser.exported)
        self.assertEqual(len(result.screenshots), 3)
        self.assertEqual(transport.payloads[0]["messages"][0]["content"], "Build it.")
        self.assertEqual(transport.payloads[1]["messages"][-1]["content"], "Add it.")

    def test_run_case_propagates_missing_artifact(self) -> None:
        transport = FakeTransport([chat_response("What theme do you want?")])
        with tempfile.TemporaryDirectory() as directory:
            result = run.run_case(
                {
                    "id": "B2",
                    "language": "en",
                    "create": "Build it.",
                    "change": "Add it.",
                    "interaction": "timer",
                    "version_pattern": "long break",
                },
                transport,
                FakeBrowser(),
                "atlas-test",
                1,
                Path(directory),
                language_detector=lambda _answer: "en",
            )
        self.assertFalse(result.success)
        self.assertEqual(result.steps[0].error, "no_artifact_link")

    def test_report_aggregates_and_handles_empty_percentiles(self) -> None:
        empty = report.aggregate([])
        self.assertEqual(empty["profiles"], {})
        self.assertEqual(
            report.markdown(empty).splitlines()[0], "# Artefact continuation benchmark"
        )

        result = {
            "profile": "atlas-test",
            "success": True,
            "language_match": True,
            "keyboard_operability": True,
            "touch_control": True,
            "console_errors": 0,
            "steps": [
                {
                    "name": name,
                    "success": True,
                    "duration_seconds": 1.0,
                    "answer": "done",
                    "usage": {
                        "cost_eur": 0.1,
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "reasoning_tokens": 3,
                    },
                }
                for name in run.STEP_NAMES
            ],
        }
        aggregate_data = report.aggregate([result])
        values = aggregate_data["profiles"]["atlas-test"]
        self.assertEqual(values["runs"], 1)
        self.assertEqual(values["full_success"], 1)
        self.assertEqual(values["first_preview_p50_seconds"], 1.0)
        self.assertEqual(values["first_preview_p90_seconds"], 1.0)
        self.assertAlmostEqual(values["cost_eur"], 0.6)
        self.assertIn("| atlas-test | 1 |", report.markdown(aggregate_data))

    def test_cases_file_has_five_contracted_cases(self) -> None:
        cases = run.load_cases(Path("evals/artifacts/cases.json"))
        self.assertEqual([case["id"] for case in cases], ["B1", "B2", "B3", "B4", "B5"])
        self.assertEqual(
            sorted({case["language"] for case in cases}), ["de", "en", "fr", "it"]
        )
        self.assertTrue(all(json.dumps(case) for case in cases))


if __name__ == "__main__":
    unittest.main()
