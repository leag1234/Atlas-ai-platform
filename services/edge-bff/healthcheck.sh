#!/usr/bin/env bash
set -Eeuo pipefail
URL="${BFF_URL:-http://localhost:8080/health}"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$URL" || echo 000)
[[ "$code" == "200" ]] || { echo "edge-bff /health = $code (attendu 200)"; exit 1; }
echo "edge-bff /health ok"
