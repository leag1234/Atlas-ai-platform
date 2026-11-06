"""Project policy ceiling tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from services.retrieval.project_policy import Policy


ROOT = Path(__file__).resolve().parents[1]


class ProjectPolicyTests(unittest.TestCase):
    def test_work_limits_allow_80_tool_calls_and_81_model_calls(self) -> None:
        policy = json.loads(
            (ROOT / "contracts/examples/project-mode/Policy.valid.json").read_text()
        )
        policy["quotas"]["per_turn"].update(tool_calls=80, model_calls=81)
        Policy.model_validate(policy)

        invalid = json.loads(json.dumps(policy))
        invalid["quotas"]["per_turn"]["tool_calls"] = 81
        with self.assertRaises(ValidationError):
            Policy.model_validate(invalid)


if __name__ == "__main__":
    unittest.main()
