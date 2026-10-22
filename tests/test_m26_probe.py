"""Operational probe alarm is based on measured failures, never report mode."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import ModuleType


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "daily_probe", "scripts/daily_probe.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class ProbeTests(unittest.TestCase):
    def test_quota_failure_keeps_provider_diagnostic(self) -> None:
        probe = module()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "write_report", side_effect=lambda report: None):
                report = probe.run(
                    lambda payload: (
                        503,
                        {
                            "error": {
                                "code": "quota_exceeded",
                                "message": "quota exhausted; Provider HTTP 432",
                            }
                        },
                    ),
                    selected={"web"},
                )
            probe.write_report(report, Path(directory))
            self.assertIs(report["web"]["status"], False)
            self.assertEqual(report["web"]["error_code"], "quota_exceeded")
            self.assertIn("432", (Path(directory) / "PROBE_FAILED.txt").read_text())

    def test_failure_marker_contains_case_and_measurements(self) -> None:
        probe = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe.write_report(
                {
                    "presentation": {
                        "status": False,
                        "seconds": 120,
                        "cost_eur": 0.08,
                        "error_code": "timeout",
                    }
                },
                root,
            )
            marker = (root / "PROBE_FAILED.txt").read_text()
            for value in ("presentation", "120", "0.08", "timeout"):
                self.assertIn(value, marker)
            probe.write_report(
                {
                    "presentation": {
                        "status": True,
                        "seconds": 20,
                        "cost_eur": 0.04,
                        "error_code": "",
                    }
                },
                root,
            )
            self.assertFalse((root / "PROBE_FAILED.txt").exists())
