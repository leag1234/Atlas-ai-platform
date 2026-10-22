#!/usr/bin/env bash
# M13 disposable cycle; credentials remain on the CPU VM.
set -Eeuo pipefail
[[ "${1:-test}" == test || "${1:-test}" == serve ]]
export GPU_WORKLOAD=image GPU_IMAGE=ubuntu_jammy_gpu_os_12
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONPATH=".:${PYTHONPATH:-}"
# Refuse before reading credentials, discovering addresses or installing cleanup.
python3 -c 'from packages.gpu_ownership import require_gpu; require_gpu()'
export ATLAS_GPU_OWNER="$(python3 -c 'from packages.gpu_ownership import gpu_owner; print(gpu_owner())')"
export GPU_MAX_EUR_H="${GPU_MAX_EUR_H:-2.00}"
export GPU_SSH_PUBLIC_KEY="${GPU_SSH_PUBLIC_KEY:-$(cat /root/.ssh/atlas_m1.pub)}"
if [[ -z "${GPU_CLIENT_IP:-}" ]]; then
export GPU_CLIENT_IP="$(scw instance server list project-id="$SCW_DEFAULT_PROJECT_ID" zone=all -o json | python3 -c 'import json,sys; rows=[r for r in json.load(sys.stdin) if r["commercial_type"].startswith("BASIC")]; assert len(rows)==1; print(next(p["address"] for p in rows[0]["public_ips"] if p["family"]=="inet"))')"
fi
known_hosts="$(mktemp)"
cleanup() { python3 infra/gpu.py down; rm -f "$known_hosts"; }
trap 'exit 143' TERM INT
python3 infra/gpu.py up
trap cleanup EXIT
address="$(cat BRAIN/gpu_ip.txt)"
image_port="$(PYTHONPATH=. python3 -c 'from packages.ports import atlas_ports; print(atlas_ports().port("terminal"))')"
remote=(ssh -i /root/.ssh/atlas_m1 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$known_hosts" -o ConnectTimeout=10 "root@$address")
# Bounded readiness polling is not an inference retry.
ready=0
for attempt in $(seq 1 90); do
  if "${remote[@]}" 'test -f /var/lib/cloud/instance/boot-finished' 2>/dev/null; then ready=1; break; fi
  sleep 5
done
[[ "$ready" == 1 ]]
scp -q -i /root/.ssh/atlas_m1 -o UserKnownHostsFile="$known_hosts" services/model-gateway/image_worker.py services/model-gateway/image-model.json "root@$address:/root/"
"${remote[@]}" "docker run -d --gpus all --name atlas-imagegen -p $image_port:8000 -v /mnt/weights:/root/.cache/huggingface -v /root/image_worker.py:/app/image_worker.py:ro -v /root/image-model.json:/app/image-model.json:ro --entrypoint /bin/bash pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385 -c 'pip install --quiet transformers==4.55.2 diffusers==0.35.1 accelerate==1.10.1 sentencepiece==0.2.1 protobuf==6.32.0 && python /app/image_worker.py'"
ready=0
for attempt in $(seq 1 180); do
  if curl --fail --silent --max-time 2 "http://$address:$image_port/health" >/dev/null; then ready=1; break; fi
  if [[ "$("${remote[@]}" 'docker inspect -f "{{.State.Running}}" atlas-imagegen')" != true ]]; then
    "${remote[@]}" 'docker logs --tail 15 atlas-imagegen'
    exit 1
  fi
  sleep 5
done
[[ "$ready" == 1 ]]
export ATLAS_IMAGE_GPU_IP="$address" ATLAS_IMAGE_GPU_EUR_H="$(python3 -c 'import json;print(json.load(open("BRAIN/gpu-cost.json"))["hourly_eur"])')"
if [[ "${1:-test}" == serve ]]; then
  echo "Image generation ready for the chat UI; automatic shutdown in 10 minutes."
  sleep 600
else
  PYTHONPATH=.:tests:services/model-gateway python3 tests/imagegen_gate.py
fi
