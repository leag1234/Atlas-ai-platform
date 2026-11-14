#!/usr/bin/env bash
# Install or update the CPU picture server on its own host (Ubuntu 24.04 with
# docker.io), as root, from a copy of infra/pictures-cpu:
#   1. build the image (engine compiled for this CPU, gate, pinned wheels);
#   2. fetch and check the weights (fetch_weights.sh);
#   3. create the shared token and a self-signed TLS certificate for this
#      host's public address, once (kept across updates);
#   4. block new outbound connections from containers (systemd unit);
#   5. (re)start the container, restarted on boot, port 8790 published.
# Usage: install.sh PUBLIC_IP [klein|zimage]
# Afterwards, copy /opt/pictures-cpu/secret/cert.pem and .../token to the
# Atlas host (see docs/21-pictures-by-code.md, section "CPU picture server").
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
address="${1:?public address of this host}"
model="${2:-klein}"
root=/opt/pictures-cpu
image=atlas-pictures-cpu:local
mkdir -p "$root/secret" "$root/weights"
docker build -q -t "$image" "$here" >/dev/null
docker image inspect "$image" --format 'image {{.Id}}'
PICTURES_CPU_IMAGE="$image" bash "$here/fetch_weights.sh" "$root/weights"
if [[ ! -s "$root/secret/token" ]]; then
  umask 077
  openssl rand -hex 32 > "$root/secret/token"
  openssl req -x509 -newkey rsa:3072 -nodes -days 1095 -subj "/CN=atlas-pictures-cpu" \
    -addext "subjectAltName=IP:$address" -keyout "$root/secret/key.pem" \
    -out "$root/secret/cert.pem" 2>/dev/null
fi
chown -R 65534:65534 "$root/secret"
chmod 0500 "$root/secret"
chmod 0400 "$root/secret"/*
cat > /etc/systemd/system/atlas-pictures-egress.service <<'UNIT'
[Unit]
Description=Block new outbound connections from containers (CPU picture server)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -C DOCKER-USER -i docker0 ! -o docker0 -m conntrack ! --ctstate ESTABLISHED,RELATED -j DROP 2>/dev/null || iptables -I DOCKER-USER -i docker0 ! -o docker0 -m conntrack ! --ctstate ESTABLISHED,RELATED -j DROP'

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now atlas-pictures-egress.service >/dev/null
systemctl restart atlas-pictures-egress.service
memory="$(( $(free -g | awk '/Mem:/{print $2}') - 3 ))g"
docker rm -f atlas-pictures-cpu >/dev/null 2>&1 || true
docker run -d --name atlas-pictures-cpu --restart unless-stopped \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 512 --memory "$memory" --user 65534:65534 \
  -p 8790:8790 -e GATE_MODEL="$model" \
  -v "$root/weights:/w:ro" -v "$root/secret:/run/pictures-secret:ro" \
  "$image" >/dev/null
for _ in $(seq 120); do
  if curl -sf --cacert "$root/secret/cert.pem" -H "Authorization: Bearer $(cat "$root/secret/token")" \
      "https://$address:8790/v1/health" | grep -q '"ready": true'; then
    echo "ready model=$model"
    openssl x509 -in "$root/secret/cert.pem" -noout -fingerprint -sha256
    exit 0
  fi
  sleep 2
done
docker logs --tail 30 atlas-pictures-cpu
exit 1
