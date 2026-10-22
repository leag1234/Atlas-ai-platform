"""Publish M15 indicators only after the complete developer API assertions."""

import json
from pathlib import Path
import unittest


def main() -> None:
    path = Path("BRAIN/eval/devapi.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    modules = [
        "test_dev_auth",
        "test_dev_quota",
        "test_dev_gateway",
        "test_dev_chat",
        "test_dev_inference",
        "test_dev_input",
        "test_dev_responses",
        "test_dev_messages",
        "test_dev_context",
    ]
    suites = [unittest.defaultTestLoader.loadTestsFromName(name) for name in modules]
    assert all(suite.countTestCases() > 0 for suite in suites)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(suites))
    if not result.wasSuccessful():
        raise SystemExit(1)
    path.write_text(
        json.dumps(
            {
                "invalid_key_refused": True,
                "quota_enforced": True,
                "tool_calls_ok": True,
                "streaming_ok": True,
                "large_context_ok": True,
                "cost_per_key_tracked": True,
                "tests_run": result.testsRun,
                "mode": "recorded_provider_and_local_http",
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
