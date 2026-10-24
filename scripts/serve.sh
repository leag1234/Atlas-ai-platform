#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if ! command -v file >/dev/null; then
  echo "Project uploads require libmagic. Install it with: sudo apt-get update && sudo apt-get install --no-install-recommends file" >&2
  exit 1
fi
atlas_secrets_file="${ATLAS_SECRETS_FILE:-../secrets.env}"
if [[ -f "$atlas_secrets_file" ]]; then set -a; source "$atlas_secrets_file"; set +a; fi
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH=".:services/model-gateway${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m services.orchestrator.serving
