#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH=".:services/model-gateway${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m uvicorn services.orchestrator.devapi:app --host 127.0.0.1 --port 8030 --no-access-log
