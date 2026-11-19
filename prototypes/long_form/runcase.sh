#!/bin/bash
# Usage: runcase.sh <case>  (prototype run, log in out/<case>/run.log)
cd /opt/atlas-src/proto-longform
export LONGFORM_OUT="${LONGFORM_OUT:-out/v6}"
# Ledger cap: 4.69 EUR spent before v6 + 6 EUR for the v6 phase.
export LONGFORM_TOTAL_BUDGET_EUR="${LONGFORM_TOTAL_BUDGET_EUR:-10.69}"
export LONGFORM_KEY_FILE=/root/atlas-keys/staging/proto-longform.key
export ATLAS_KIWIX_URL="$(grep -E '^ATLAS_KIWIX_URL=' /opt/atlas-src/staging.env | cut -d= -f2-)"
export ATLAS_KIWIX_BOOKS="$(grep -E '^ATLAS_KIWIX_BOOKS=' /opt/atlas-src/staging.env | cut -d= -f2-)"
# VM rule of 2026-11-19 (memory incident): one case at a time, only with at least 3 GB available,
# under the shared agent test lock.
until [ "$(free -m | awk '/^Mem:/ {print $7}')" -ge 3072 ]; do echo "waiting: $(free -m | awk '/^Mem:/ {print $7}') MB available" >&2; sleep 30; done
PYTHONPATH=/opt/atlas-src/proto-longform:/opt/atlas-src/wt-longform exec flock /run/lock/atlas-agent-tests.lock /opt/atlas-src/repo/.venv/bin/python run.py "$1" "cases/$1.txt"
