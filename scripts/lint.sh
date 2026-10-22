#!/usr/bin/env bash
set -Eeuo pipefail
python3 -m ruff check services packages infra/gpu.py scripts/verify_m0.py evals/harness.py evals/agent.py evals/__init__.py evals/calibration.py evals/execution.py evals/reporting.py evals/suites.py evals/decision.py tests
python3 -m ruff format --check services packages infra/gpu.py scripts/verify_m0.py evals/harness.py evals/agent.py evals/__init__.py evals/calibration.py evals/execution.py evals/reporting.py evals/suites.py evals/decision.py tests
