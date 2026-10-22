#!/usr/bin/env bash
set -Eeuo pipefail

repo=/opt/atlas-src/repo
stable=/opt/atlas-src/stable
previous=/opt/atlas-src/stable-previous
[[ -d "$previous" ]] || { echo "no retained stable worktree to roll back to" >&2; exit 1; }
sudo systemctl stop atlas-stable.service
failed=/opt/atlas-src/stable-failed-$(date -u +%Y%m%dT%H%M%SZ)
git -C "$repo" worktree move "$stable" "$failed"
git -C "$repo" worktree move "$previous" "$stable"
sudo systemctl start atlas-stable.service
ready=false
for attempt in $(seq 1 60); do
  if "$stable/infra/stable/healthcheck.sh" --ready; then ready=true; break; fi
  sleep 2
done
"$ready" || { echo "rollback startup failed" >&2; exit 1; }
"$stable/infra/stable/healthcheck.sh"
git -C "$repo" worktree remove --force "$failed"
git -C "$stable" describe --tags --exact-match > /tmp/atlas-stable-tag
sudo install -m 0644 /tmp/atlas-stable-tag /opt/atlas-src/stable-last-deploy
rm -f /tmp/atlas-stable-tag
