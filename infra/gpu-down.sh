#!/usr/bin/env bash
# infra/gpu-down.sh delegates to the single engine infra/gpu.py, enforcing the 6
# guarantees in the fixed specification (atlas-m1 tag + project ownership, validated JSON,
# propagated errors, terminate --wait, rejection of ambiguity). One implementation.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; [[ -f .env ]] && source .env; set +a
exec python3 infra/gpu.py down "$@"
