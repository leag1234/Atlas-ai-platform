#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
atlas_secrets_file="${ATLAS_SECRETS_FILE:-../secrets.env}"
if [[ -f "$atlas_secrets_file" ]]; then set -a; source "$atlas_secrets_file"; set +a; fi
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH=".:services/model-gateway${PYTHONPATH:+:$PYTHONPATH}"
if [[ -f package-lock.json && ! -d node_modules ]]; then
  npm ci --ignore-scripts --omit=dev --no-audit --no-fund
fi
exec python3 -m services.orchestrator.serving
