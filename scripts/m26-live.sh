#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -f ../secrets.env ]]; then set -a; source ../secrets.env; set +a; fi
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PATH="$PWD/.venv/bin:$PATH" PYTHONPATH=".:tests:services/model-gateway"
case "${M26_CASES:-all}" in
  J54|J59) atlas_providers=(tavily); atlas_searches=(3) ;;
  all) atlas_providers=(tavily serpapi); atlas_searches=(9 18) ;;
  *) atlas_providers=(serpapi); atlas_searches=(18) ;;
esac
for atlas_index in "${!atlas_providers[@]}"; do
  python3 scripts/search_allowance.py "${atlas_providers[$atlas_index]}" before-acceptance "${atlas_searches[$atlas_index]}"
done
audit_after() {
  local atlas_status=0 atlas_provider
  for atlas_provider in "${atlas_providers[@]}"; do
    python3 scripts/search_allowance.py "$atlas_provider" after-acceptance || atlas_status=1
  done
  return "$atlas_status"
}
trap 'atlas_exit=$?; audit_after || atlas_exit=1; exit "$atlas_exit"' EXIT
python3 tests/m26_live.py
