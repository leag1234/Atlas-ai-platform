#!/usr/bin/env bash
set -Eeuo pipefail
# Use the project virtualenv when present: dependencies are installed there,
# so tests must run with the same interpreter (otherwise ModuleNotFoundError).
if [[ "$(command -v python3)" == /usr/bin/python3 && -x "$PWD/.venv/bin/python3" ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi
# Tests never start or manage a GPU: give the suite a placeholder image worker
# address (nothing listens there) so image routes run against fakes and recordings.
# Tests of the unconfigured case clear these variables themselves.
export ATLAS_IMAGE_GPU_IP="${ATLAS_IMAGE_GPU_IP:-127.0.0.9}"
export ATLAS_IMAGE_GPU_EUR_H="${ATLAS_IMAGE_GPU_EUR_H:-0.7875}"
export PYTHONPATH="services/model-gateway:services/edge-bff${PYTHONPATH:+:$PYTHONPATH}"
# The protected workflow calls make test; execute the full milestone gate there.
if [[ "${GITHUB_ACTIONS:-}" == true && "${ATLAS_VERIFY_M0:-}" != 1 ]]; then
  # The workflow may split the gate into parallel shards (ATLAS_CI_SHARD):
  # "unit" = M0 to M4, "milestones" = tests/ci_regressions.py restricted by
  # ATLAS_CI_MILESTONES. Without a shard the complete gate runs, as before.
  shard="${ATLAS_CI_SHARD:-all}"
  if [[ "$shard" == all || "$shard" == unit ]]; then
    # M0 calls make test with ATLAS_VERIFY_M0=1: it owns the complete unit run.
    make verify-m0
    PYTHONPATH=".:tests:$PYTHONPATH" python3 tests/m2_gate.py
    ATLAS_M3_EVAL_MODE=replay make verify-m3
    make verify-m4
  fi
  if [[ "$shard" == all || "$shard" == milestones ]]; then
    python3 tests/ci_regressions.py
  fi
else
  python3 -m unittest discover -s tests -v
fi
