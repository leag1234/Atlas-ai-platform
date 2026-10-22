"""No-network tests for transport, durable reservations and startup safety."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from packages.ports import DEFAULT_PORTS
from unittest.mock import patch

from evals.truthfulness.run import Ledger, ask, busy_ports, audit_batch, run
from evals.truthfulness.score import load_cases


class TruthfulnessRunnerTests(unittest.TestCase):
    def test_occupied_ports_prevent_all_startup_and_paid_calls(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("evals.truthfulness.run.busy_ports", return_value=[8000, 8020]),
            patch("evals.truthfulness.run.public_stack") as stack,
            patch("evals.truthfulness.run.post") as post,
            patch("evals.truthfulness.run.audit_batch") as audit,
        ):
            output = Path(directory) / "pending"
            self.assertEqual(run(output, "offline-unit-test"), 2)
            report = json.loads((output / "results.json").read_text())
            self.assertEqual(report["mode"], "pending")
            self.assertEqual(report["answers"], [])
            self.assertEqual(report["reason"], "ports_busy:8000,8020")
            stack.assert_not_called()
            post.assert_not_called()
            audit.assert_not_called()

    def test_three_transport_failures_stop_across_profiles(self) -> None:
        case = load_cases(Path("evals/truthfulness/cases.json"))[0]
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.json")
            with patch("evals.truthfulness.run.post", return_value=(503, {})) as post:
                ask(case, "atlas-qwen", ledger)
                second = ask(case, "atlas-glm", ledger)
            self.assertEqual(post.call_count, 3)
            self.assertEqual(second["error"], "three_consecutive_identical_failures")

    def test_transport_retry_retains_failed_reservation(self) -> None:
        case = load_cases(Path("evals/truthfulness/cases.json"))[0]
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.json")
            success = {
                "choices": [{"message": {"content": "31 March 1889"}}],
                "atlas": {"cost_eur": 0.01},
                "usage": {},
            }
            with patch(
                "evals.truthfulness.run.post", side_effect=[(503, {}), (200, success)]
            ) as post:
                result = ask(case, "atlas-qwen", ledger)
            self.assertEqual(post.call_count, 2)
            self.assertIsNone(result["error"])
            self.assertEqual(str(ledger.spent), "0.11")
            self.assertEqual([r["status"] for r in ledger.rows], [503, 200])
            self.assertEqual([r["attempt"] for r in ledger.rows], [1, 2])
            self.assertEqual(Ledger(ledger.path).spent, ledger.spent)
            with self.assertRaisesRegex(RuntimeError, "already_attempted"):
                ask(case, "atlas-qwen", ledger)

    def test_quality_and_client_errors_are_never_retried(self) -> None:
        case = load_cases(Path("evals/truthfulness/cases.json"))[0]
        for status, payload, expected in (
            (400, {}, "http_400"),
            (200, {"choices": [{"message": {"content": "wrong"}}]}, None),
            (200, {}, "invalid_response"),
            (429, {"error": {"code": "quota_exhausted"}}, "quota_exhausted"),
        ):
            with (
                self.subTest(status=status, payload=payload),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = Ledger(Path(directory) / "ledger.json")
                with patch(
                    "evals.truthfulness.run.post", return_value=(status, payload)
                ) as post:
                    result = ask(case, "atlas-qwen", ledger)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(result["error"], expected)
                self.assertEqual(str(ledger.spent), "0.10")

    def test_no_third_transport_attempt(self) -> None:
        case = load_cases(Path("evals/truthfulness/cases.json"))[0]
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.json")
            with patch("evals.truthfulness.run.post", return_value=(502, {})) as post:
                result = ask(case, "atlas-glm", ledger)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(result["error"], "http_502")

    def test_ledger_cap_and_invalid_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.json")
            for i in range(30):
                ledger.reserve(str(i), "atlas-qwen", 1)
            self.assertEqual(str(ledger.spent), "3.00")
            with self.assertRaisesRegex(RuntimeError, "ledger_cap"):
                ledger.reserve("over", "atlas-qwen", 1)
            for bad in ("NaN", "Infinity", "-1", None):
                ledger.settle(0, 200, {"atlas": {"cost_eur": bad}})
                self.assertEqual(str(ledger.spent), "3.00")

    def test_ports_parse_wildcard_ipv4_ipv6(self) -> None:
        terminal, adapter = DEFAULT_PORTS["terminal"], DEFAULT_PORTS["adapter"]
        with (
            patch.dict(os.environ, {"ATLAS_PORT_BASE": "0"}),
            patch("evals.truthfulness.run.subprocess.run") as run,
        ):
            run.return_value.stdout = (
                f"LISTEN 0 5 127.0.0.1:{terminal} 0.0.0.0:*\n"
                f"LISTEN 0 5 [::]:{adapter} [::]:*\n"
                f"LISTEN 0 5 127.0.0.1:1{adapter} *:*\n"
            )
            self.assertEqual(busy_ports(), [terminal, adapter])
            run.assert_called_once_with(
                ["ss", "-ltnH"], check=True, capture_output=True, text=True
            )

    def test_allowance_uses_script_with_worst_case_and_audits_both(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "tavily"}, clear=True),
            patch("evals.truthfulness.run.subprocess.run") as run,
        ):
            run.return_value.stdout = json.dumps({"allowed": True, "remaining": 806})
            run.return_value.returncode = 0
            audit_batch(Path(directory), "before-1", "tavily", 90)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                [c[-3:] for c in commands],
                [
                    ["tavily", "truthfulness-before-1", "90"],
                    ["serpapi", "truthfulness-before-1", "0"],
                ],
            )
            self.assertTrue((Path(directory) / "allowance.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
