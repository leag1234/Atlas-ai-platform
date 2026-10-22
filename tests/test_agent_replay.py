"""Replay cannot silently accept changed prompts, calls or incomplete records."""

import unittest
import yaml
import gzip
import json
import tempfile
from pathlib import Path
from decimal import Decimal
from typing import Any
from agent_gate_eval import ReplayModel, ReplayTools, select_cases, load_record, replay
from services.orchestrator.loop import Call


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_stop_keeps_measured_diagnostic_and_exact_result(self) -> None:
        cases = yaml.safe_load(Path("evals/golden/e4_tool_calling.yaml").read_text())
        case = next(c for c in cases if c["id"] == "E4-020")
        results = await replay(Path("tests/cassettes/agent"), [case])
        self.assertIn("Tokens reserved: 8360; limit 16384", results[0].text)
        self.assertIn("Elapsed: 0.000 seconds; limit 120.000 seconds", results[0].text)
        record = load_record(Path("tests/cassettes/agent"), case["id"])
        record["result"]["tokens"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / (case["id"] + ".json")).write_text(json.dumps(record))
            with self.assertRaisesRegex(AssertionError, "recorded_result_changed"):
                await replay(path, [case])

    def record(self) -> dict[str, Any]:
        return {
            "configuration": {
                "input_eur_per_mtok": "0.6",
                "output_eur_per_mtok": "3.6",
                "max_tokens": 2048,
            },
            "model": [
                {
                    "messages": [{"role": "user", "content": "test"}],
                    "prompt_tokens": 20,
                    "response": {
                        "text": "answer",
                        "calls": [],
                        "usage": {"tokens": 22, "cost": "0.0000192"},
                    },
                }
            ],
            "tools": [
                {
                    "call": {
                        "id": "1",
                        "name": "calculator",
                        "arguments": '{"expr":"1+1"}',
                    },
                    "output": {"value": "2"},
                }
            ],
        }

    async def test_exact_replay_and_usage(self) -> None:
        model = ReplayModel(self.record())
        turn = await model.complete([{"role": "user", "content": "test"}], 1)
        self.assertEqual(turn.text, "answer")
        self.assertEqual(model.prompt_tokens, 20)
        self.assertIsNotNone(turn.usage)
        assert turn.usage is not None
        self.assertEqual(turn.usage.cost, Decimal("0.0000192"))
        tools = ReplayTools(self.record())
        self.assertEqual(
            await tools.execute(Call("1", "calculator", '{"expr":"1+1"}'), 1),
            {"value": "2"},
        )

    async def test_changed_request_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssertionError, "unrecorded_model_request"):
            await ReplayModel(self.record()).complete(
                [{"role": "user", "content": "changed"}], 1
            )
        with self.assertRaisesRegex(AssertionError, "unrecorded_tool_request"):
            await ReplayTools(self.record()).execute(Call("1", "web_search", "{}"), 1)

    def test_only_validated_web_cases_are_executed(self) -> None:
        cases = [
            {"id": "valid", "statut": "valide"},
            {"id": "pending", "statut": "draft"},
            {"id": "expired", "statut": "stale"},
        ]
        self.assertEqual(select_cases("web", cases), [cases[0]])
        self.assertEqual(select_cases("tools", cases), cases)
        with self.assertRaisesRegex(ValueError, "no_validated_web_cases"):
            select_cases("web", cases[1:])

    def test_compressed_recording_preserves_exact_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with gzip.open(path / "case.json.gz", "wt", encoding="utf-8") as stream:
                json.dump(self.record(), stream)
            self.assertEqual(load_record(path, "case"), self.record())
            (path / "invalid.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "invalid_recording"):
                load_record(path, "invalid")
