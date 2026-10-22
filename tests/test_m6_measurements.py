import asyncio
import gzip
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import yaml
import m6_bench
import m6_demo
from agent_gate_eval import load_record


class MeasurementTests(unittest.TestCase):
    def test_benchmark_archive_and_budget_precondition(self) -> None:
        record = {"telemetry": {"cost": 0.001}}
        with (
            patch(
                "m6_bench.subprocess.run",
                side_effect=RuntimeError("budget test failed"),
            ),
            patch.object(m6_bench, "probe") as probe,
        ):
            with self.assertRaises(RuntimeError):
                m6_bench.main()
            probe.assert_not_called()
        cwd = Path.cwd()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("m6_bench.subprocess.run"),
            patch.object(m6_bench, "probe", return_value=record),
            patch("m6_bench.multiprocessing.get_context") as context,
        ):
            try:
                os.chdir(tmp)
                Path("BRAIN/eval").mkdir(parents=True)
                Path("reports").mkdir()
                context.return_value.Pool.return_value.__enter__.return_value.map.return_value = (
                    [record] * 8
                )
                m6_bench.main()
                archived = json.loads(
                    gzip.decompress(Path("reports/bench.json.gz").read_bytes())
                )
                self.assertEqual([len(w) for w in archived["waves"]], [8, 8])
                self.assertTrue(archived["budget_tests"])
            finally:
                os.chdir(cwd)

    def test_live_recording_isolated_and_logger_restored(self) -> None:
        cases = yaml.safe_load(Path("tests/journeys/demo.yaml").read_text())
        records = {
            c["id"]: load_record(Path("tests/cassettes/demo"), c["id"]) for c in cases
        }
        logger = logging.getLogger("agent_provider")
        level, handlers = logger.level, list(logger.handlers)

        async def recorded(case: dict[str, str], url: str) -> str:
            data = records[case["id"]]
            Path("BRAIN/agent-recordings", case["id"] + ".json").write_text(
                json.dumps(data)
            )
            logger.info(json.dumps({"fallback": data["fallback"]}))
            return ""

        cwd = Path.cwd()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                m6_demo, "record", new=AsyncMock(side_effect=recorded)
            ) as call,
        ):
            try:
                os.chdir(tmp)
                Path("BRAIN/agent-recordings").mkdir(parents=True)
                Path("tests/journeys").mkdir(parents=True)
                Path("reports").mkdir()
                Path("tests/journeys/demo.yaml").write_text(yaml.safe_dump(cases))
                asyncio.run(m6_demo.live(cases, "http://unused.invalid"))
                m6_demo.archive_measurements()
                self.assertEqual(
                    len(
                        json.loads(
                            gzip.decompress(Path("reports/demo.json.gz").read_bytes())
                        )
                    ),
                    3,
                )
                self.assertEqual(call.await_count, 3)
                self.assertEqual(
                    len(list(Path("tests/cassettes/demo").glob("*.gz"))), 3
                )
                self.assertEqual(logger.handlers, handlers)
                self.assertEqual(logger.level, level)
            finally:
                os.chdir(cwd)

    def test_demo_failure_cleans_up_database_and_server(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_M6_MODE": "record"}),
            patch("sys.argv", ["m6_demo.py"]),
            patch.object(m6_demo, "StorageTests") as storage,
            patch.object(m6_demo, "Store"),
            patch.object(m6_demo, "CPUModels"),
            patch.object(m6_demo, "serve") as serve,
            patch.object(m6_demo, "ingest"),
            patch.object(
                m6_demo, "live", new=AsyncMock(side_effect=RuntimeError("failed"))
            ),
        ):
            serve.return_value.__enter__.return_value.server_port = 1234
            storage.dsn = "test-only"
            with self.assertRaises(RuntimeError):
                m6_demo.main()
            storage.doClassCleanups.assert_called_once()
            serve.return_value.__enter__.return_value.shutdown.assert_called_once()
