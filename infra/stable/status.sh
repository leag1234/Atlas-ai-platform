#!/usr/bin/env bash
set -Eeuo pipefail

repo=/opt/atlas-src/repo
stable=/opt/atlas-src/stable
printf 'last deploy: '; [[ -f /opt/atlas-src/stable-last-deploy ]] && cat /opt/atlas-src/stable-last-deploy || echo unknown
printf 'running tag: '; git -C "$stable" describe --tags --exact-match 2>/dev/null || echo unknown
systemctl --no-pager --full status atlas-stable.service | sed -n '1,12p'
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -- '-100' || true
printf 'today spend: '
if [[ -x "$repo/infra/inventory.sh" ]]; then "$repo/infra/inventory.sh"; else echo 'not available locally'; fi
