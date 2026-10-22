#!/usr/bin/env bash
# infra/gpu-up.sh delegates to the single engine infra/gpu.py, enforcing the
# GPU_MAX_EUR_H ceiling (rejecting types above the hourly limit) and selecting
# an available compatible type. One implementation means one guardrail.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; [[ -f .env ]] && source .env; set +a
: "${GPU_MAX_EUR_H:?GPU_MAX_EUR_H must be set (hourly ceiling, e.g. 2.00)}"
exec python3 infra/gpu.py up "$@"
