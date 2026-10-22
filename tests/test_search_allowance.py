"""Account reserve checks stop before paid batches and keep credentials out of logs."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class AllowanceTests(unittest.TestCase):
    def test_launcher_audits_used_providers_even_after_one_audit_fails(self) -> None:
        for selection, failure, providers in (
            ("J59", "", ["tavily"]),
            ("web", "", ["serpapi"]),
            ("all", "after-acceptance", ["tavily", "serpapi"]),
        ):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                scripts = root / "scripts"
                scripts.mkdir()
                launcher = scripts / "m26-live.sh"
                launcher.write_bytes(Path("scripts/m26-live.sh").read_bytes())
                binary = root / ".venv/bin/python3"
                binary.parent.mkdir(parents=True)
                binary.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, pathlib, sys\n"
                    "with pathlib.Path('calls.jsonl').open('a') as log:\n"
                    "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "sys.exit(1 if len(sys.argv)>3 and sys.argv[2]=='tavily' "
                    "and sys.argv[3]==os.environ['AUDIT_FAILURE'] else 0)\n"
                )
                binary.chmod(0o700)
                completed = subprocess.run(
                    ["bash", str(launcher)],
                    env={
                        **os.environ,
                        "M26_CASES": selection,
                        "AUDIT_FAILURE": failure,
                    },
                    capture_output=True,
                    timeout=10,
                )
                calls = [
                    json.loads(line)
                    for line in (root / "calls.jsonl").read_text().splitlines()
                ]
                for stage in ("before-acceptance", "after-acceptance"):
                    self.assertEqual(
                        [
                            call[1]
                            for call in calls
                            if len(call) > 2 and call[2] == stage
                        ],
                        providers,
                    )
                self.assertIn(["tests/m26_live.py"], calls)
                self.assertEqual(completed.returncode, 1 if failure else 0)

    def test_provider_reserves_and_sanitized_log(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "search_allowance", "scripts/search_allowance.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for provider, balance, expected in (
            ("tavily", 155, False),
            ("tavily", 156, True),
            ("serpapi", 52, False),
            ("serpapi", 53, True),
        ):
            data = {
                "account": {"plan_limit": balance, "plan_usage": 0},
                "total_searches_left": balance,
            }
            with (
                self.subTest(provider=provider, balance=balance),
                tempfile.TemporaryDirectory() as directory,
                patch.object(module, "Path", return_value=Path(directory)),
                patch.dict(
                    module.os.environ,
                    {"TAVILY_API_KEY": "test-only-key", "SERPAPI_KEY": "test-only-key"},
                ),
                patch.object(
                    module,
                    "urlopen",
                    return_value=io.BytesIO(json.dumps(data).encode()),
                ),
            ):
                self.assertIs(module.audit(provider, "before-probe", 3), expected)
                log = (Path(directory) / "search-allowance.jsonl").read_text()
                self.assertNotIn("test-only-key", log)
                self.assertEqual(json.loads(log)["remaining"], balance)
                self.assertEqual(
                    (Path(directory) / "PROBE_FAILED.txt").exists(), not expected
                )
