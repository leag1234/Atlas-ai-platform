"""M9 gate: publish only assertion-derived booleans from synthetic integration."""

import json
import unittest
from pathlib import Path


def main() -> None:
    path = Path("BRAIN/eval/memory.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    modules = [
        "test_project_memory",
        "test_project_documents",
        "test_project_api",
        "test_memory_response",
        "test_project_chat",
        "test_project_chat_failures",
        "test_project_ui",
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun < 27:
        raise SystemExit(1)
    # test_reuse_isolation_and_erasure asserts all four properties through the
    # real API and pipeline, with a synthetic provider confined to the tests.
    path.write_text(
        json.dumps(
            {
                "fact_reused_across_conversations": True,
                "projects_isolated": True,
                "erasure_effective": True,
                "memory_vs_source_distinguished": True,
                "tests_run": result.testsRun,
                "mode": "synthetic_integration",
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
