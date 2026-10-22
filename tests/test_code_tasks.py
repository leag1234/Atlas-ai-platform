"""Regression checks for the offline Code mode evaluation fixtures."""

from __future__ import annotations

import sys
import unittest

from evals.code_tasks.run import load_tasks, null_agent, oracle_agent, run_all, run_task


class CodeTaskTests(unittest.TestCase):
    def test_task_set_references_are_deterministic(self) -> None:
        tasks = load_tasks()
        self.assertEqual(len(tasks), 30)
        self.assertEqual(
            {task.identifier for task in tasks},
            {task.identifier for task in load_tasks()},
        )
        for task in tasks:
            self.assertLess(task.archive.stat().st_size, 200_000)
            first = run_task(task, oracle_agent, sys.executable)
            second = run_task(task, oracle_agent, sys.executable)
            self.assertTrue(first["solved"], first["verifier_output"])
            self.assertTrue(second["solved"], second["verifier_output"])

    def test_reference_agents_define_the_score_bounds(self) -> None:
        oracle = run_all(oracle_agent)
        null = run_all(null_agent)
        self.assertEqual(oracle["solved_count"], oracle["task_count"])
        self.assertEqual(oracle["false_claim_count"], 0)
        self.assertEqual(null["solved_count"], 0)


if __name__ == "__main__":
    unittest.main()
