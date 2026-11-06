# Task 3: only Work turns turn a budget refusal into a named stop

## What changed

- Restricted the `cost_budget` conversion to runs whose `Limits.code_mode` is true.
- Kept `GatewayError` in `services/orchestrator/loop.py` and its re-export from `services/orchestrator/model.py`.
- Kept the existing Work-mode budget-stop test.
- Added a non-code-mode regression proving `GatewayError("cost_budget", 429, ...)` still raises instead of returning a stopped result.

## Evidence

- `PATH=/opt/atlas-src/repo/.venv/bin:$PATH PYTHONPATH=services/model-gateway:services/edge-bff:.:tests python -m unittest tests.test_work_failure_message tests.test_budgets tests.test_m21_quality tests.test_streaming_lifecycle`
  - Result: `Ran 32 tests ... OK`
- `PATH=/opt/atlas-src/repo/.venv/bin:$PATH ATLAS_PORT_BASE=18000 make test-streaming`
  - Result: `Ran 38 tests ... OK`
- `PATH=/opt/atlas-src/repo/.venv/bin:$PATH make lint`
  - Result: `All checks passed!` and `715 files already formatted`
- `PATH=/opt/atlas-src/repo/.venv/bin:$PATH make typecheck`
  - Result: `Success: no issues found in 715 source files`

The first lint invocation found formatting in the new test. Formatting the test file and rerunning lint and typecheck passed. This was a local formatting correction, not a product/provider attempt.

## Cost and cleanup

- Model/provider cost: EUR 0.00; all validation used local tests and no live model calls.
- Cloud resources: none created.
- Containers, networks, anonymous volumes, task-built images, temporary files, and download caches: none created by this task. Existing containers and temporary files were not touched because they were not created by this task.
- Space freed: 0 bytes; no task artifacts were created outside the repository.

## Scope

- No protected files, prompts, corpus cases, thresholds, or assertions were modified.
- No push, PR, merge, or branch creation was performed.
- No `make verify-m26` or `tests/ci_regressions.py` run was performed.
