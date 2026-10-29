#!/usr/bin/env bash
# Hold demo capacity through transient worker failure; release at the deadline.
set -Eeuo pipefail
deadline=${1:?deadline epoch}
log() { echo "$(date -u +%FT%TZ) $*"; }
export ATLAS_GPU_OWNER=demo GPU_WORKLOAD=image GPU_IMAGE=ubuntu_jammy_gpu_os_12
# Set ATLAS_GPU_ROOT when installing these scripts alongside another checkout.
cd "${ATLAS_GPU_ROOT:-/opt/atlas-src/wt-gpuown}"
if [[ -f "${ATLAS_GPU_ENV_FILE:-/opt/atlas-src/secrets.env}" ]]; then
  set -a
  source "${ATLAS_GPU_ENV_FILE:-/opt/atlas-src/secrets.env}"
  set +a
fi
export ATLAS_GPU_OWNER=demo ATLAS_STABLE=1
export PATH="$PWD/.venv/bin:$PATH" PYTHONPATH=".:${PYTHONPATH:-}"
export GPU_MAX_EUR_H="${GPU_MAX_EUR_H:-2.00}"
export GPU_SSH_PUBLIC_KEY="${GPU_SSH_PUBLIC_KEY:-$(cat /root/.ssh/atlas_m1.pub)}"
if [[ -z "${GPU_CLIENT_IP:-}" ]]; then
  GPU_CLIENT_IP="$(scw instance server list project-id="$SCW_DEFAULT_PROJECT_ID" zone=all -o json | python3 -c 'import json,sys; rows=[r for r in json.load(sys.stdin) if r["commercial_type"].startswith("BASIC")]; assert len(rows)==1; print(next(p["address"] for p in rows[0]["public_ips"] if p["family"]=="inet"))')"
  export GPU_CLIENT_IP
fi
known_hosts="$(mktemp)"
flag() {
  if [[ -n "${ATLAS_GPU_FLAG_SCRIPT:-}" ]]; then
    "$ATLAS_GPU_FLAG_SCRIPT" "$1"
    return
  fi
  # Keep the demo toggle local to this checkout's rate, not another worktree.
  python3 - "$1" <<'PYFLAG'
import json
from pathlib import Path
import sys
path = Path("/opt/atlas-src/stable.env")
keys = ("ATLAS_STABLE_IMAGES=", "ATLAS_IMAGE_GPU_IP=", "ATLAS_IMAGE_GPU_EUR_H=")
lines = [line for line in path.read_text().splitlines() if not line.startswith(keys)]
if sys.argv[1] == "on":
    rate = json.loads(Path("BRAIN/gpu-cost.json").read_text())["hourly_eur"]
    lines.extend(["ATLAS_STABLE_IMAGES=1", "ATLAS_IMAGE_GPU_IP=127.0.0.2", f"ATLAS_IMAGE_GPU_EUR_H={rate}"])
path.write_text("\n".join(lines) + "\n")
PYFLAG
  systemctl restart atlas-stable
}
cleanup() {
  local rc=$?
  trap - EXIT
  [[ -z "${forward:-}" ]] || kill "$forward" 2>/dev/null || true
  flag off || true
  if (( $(date +%s) >= deadline )); then
    log "deadline reached: deleting owned demo resources"
    python3 infra/gpu.py down || rc=1
    rm -f "BRAIN/gpu-window-reboot-$deadline"
  else
    log "window interrupted before deadline: preserving owned capacity for recovery"
  fi
  rm -f "$known_hosts"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 143' TERM INT
if (( $(date +%s) >= deadline )); then exit 0; fi
log "attempt: resume healthy or recoverable owned server; create only if absent"
python3 infra/gpu.py resume
address="$(cat BRAIN/gpu_ip.txt)"
server_id="$(python3 infra/gpu.py status | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
reboot_marker="BRAIN/gpu-window-reboot-$deadline"
image_port="$(python3 -c 'from packages.ports import atlas_ports; print(atlas_ports().port("terminal"))')"
remote=(ssh -i /root/.ssh/atlas_m1 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$known_hosts" -o ConnectTimeout=10 "root@$address")
health() { curl --fail --silent --max-time 5 "http://$address:$image_port/health" >/dev/null; }
wait_health() {
  local until=$(( $(date +%s) + $1 ))
  while (( $(date +%s) < until && $(date +%s) < deadline )); do
    if health; then log "worker health restored; retaining server"; return 0; fi
    sleep 20
  done
  return 1
}
recover() {
  log "worker health lost: retaining server and retrying health for up to 600 seconds"
  wait_health 600 && return 0
  (( $(date +%s) < deadline )) || return 1
  # Revalidate ownership before SSH mutation as well as provider actions.
  python3 infra/gpu.py check >/dev/null || return 1
  log "health retry exhausted: restarting worker container over SSH"
  timeout 90 "${remote[@]}" 'docker restart atlas-imagegen' || log "worker restart failed"
  wait_health 120 && return 0
  (( $(date +%s) < deadline )) || return 1
  if [[ -f "$reboot_marker" && "$(cat "$reboot_marker")" == "$server_id" ]]; then
    log "owned server reboot already attempted in this window; retaining capacity while retrying health"
  else
    log "worker restart recovery exhausted: rebooting owned server once"
    printf '%s\n' "$server_id" > "$reboot_marker"
    python3 infra/gpu.py reboot || log "owned server reboot failed"
  fi
  wait_health 180 && return 0
  (( $(date +%s) < deadline )) || return 1
  if python3 infra/gpu.py capacity; then
    log "recoveries exhausted and same-type capacity available: deleting for replacement"
    python3 infra/gpu.py down
  else
    log "deletion blocked: same-type capacity unavailable or unknown; retaining server"
  fi
  return 1
}
# Reuse an installed worker. Deploy only when the owned server has none.
if ! health; then
  booted=0
  for attempt in $(seq 1 90); do
    (( $(date +%s) < deadline )) || exit 0
    if "${remote[@]}" 'test -f /var/lib/cloud/instance/boot-finished'; then booted=1; break; fi
    sleep 5
  done
  if [[ "$booted" == 1 ]] && ! "${remote[@]}" 'docker inspect atlas-imagegen' >/dev/null 2>&1; then
    log "owned server has no worker: deploying container"
    python3 infra/gpu.py check >/dev/null
    scp -q -i /root/.ssh/atlas_m1 -o UserKnownHostsFile="$known_hosts" services/model-gateway/image_worker.py services/model-gateway/image-model.json services/model-gateway/image-klein.json services/model-gateway/requirements-image-klein.txt "root@$address:/root/"
    "${remote[@]}" "docker run -d --restart unless-stopped --gpus all --name atlas-imagegen -p $image_port:8000 -e ATLAS_IMAGE_PROFILE=\"${ATLAS_IMAGE_PROFILE:-klein}\" -e HF_HOME=/root/.cache/huggingface -v /mnt/weights:/root/.cache/huggingface -v /root/image_worker.py:/app/image_worker.py:ro -v /root/image-model.json:/app/image-model.json:ro -v /root/image-klein.json:/app/image-klein.json:ro -v /root/requirements-image-klein.txt:/app/requirements-image-klein.txt:ro --entrypoint /bin/bash pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385 -c 'pip install --quiet -r /app/requirements-image-klein.txt && python /app/image_worker.py'" || log "worker deployment failed; starting recovery"
  fi
  health || recover || exit 1
fi
log "worker ready; establishing demo forward"
ssh -i /root/.ssh/atlas_m1 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$known_hosts" -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -N -L "127.0.0.2:8100:localhost:$image_port" "root@$address" &
forward=$!
fwd=0
for attempt in $(seq 1 20); do
  if curl --fail --silent --max-time 5 http://127.0.0.2:8100/health >/dev/null; then fwd=1; break; fi
  kill -0 "$forward" 2>/dev/null || break
  sleep 3
done
[[ "$fwd" == 1 ]] || { log "forward failed; retain server for next attempt"; exit 1; }
flag on
while (( $(date +%s) < deadline )); do
  sleep 60
  (( $(date +%s) < deadline )) || break
  if ! health; then
    flag off
    recover || exit 1
    flag on
  fi
done
log "window complete"
