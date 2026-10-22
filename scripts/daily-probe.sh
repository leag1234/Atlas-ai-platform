#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -f ../secrets.env ]]; then set -a; source ../secrets.env; set +a; fi
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PATH="$PWD/.venv/bin:$PATH" PYTHONPATH=".:tests:services/model-gateway"
export ATLAS_SEARCH_PROVIDER=serpapi
exec 9>BRAIN/daily-probe.lock
flock -n 9
python3 scripts/search_allowance.py serpapi before-probe 18
trap 'python3 scripts/search_allowance.py serpapi after-probe' EXIT
M26_CASES=presentation,owner_pdf,docx,xlsx,web,plain python3 tests/m26_live.py
