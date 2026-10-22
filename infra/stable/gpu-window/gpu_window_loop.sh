#!/usr/bin/env bash
# Retry preserved demo capacity until the window deadline, then release it.
set -uo pipefail
deadline=${1:?deadline epoch}
export ATLAS_GPU_OWNER=demo ATLAS_STABLE=1
window="$(dirname "${BASH_SOURCE[0]}")/gpu_window.sh"
log() { echo "$(date -u +%FT%TZ) $*"; }
log "loop start, deadline $deadline"
while (( $(date +%s) < deadline )); do
  log "window attempt: existing owned capacity will be retained"
  bash "$window" "$deadline"
  rc=$?
  log "attempt ended rc=$rc"
  remaining=$(( deadline - $(date +%s) ))
  (( remaining > 0 )) || break
  (( remaining > 300 )) && remaining=300
  sleep "$remaining"
done
log "deadline reached: final owned-resource cleanup"
bash "$window" "$deadline"
rc=$?
log "loop end rc=$rc"
exit "$rc"
