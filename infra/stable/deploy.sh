#!/usr/bin/env bash
set -Eeuo pipefail

tag=${1:?usage: deploy.sh TAG}
[[ "$tag" =~ ^stable-[0-9]{8}-[0-9]+$ ]] || { echo "invalid stable tag" >&2; exit 1; }
runtime=/opt/atlas-src/stable-python
[[ -x "$runtime/bin/python3" ]] || { echo "prepare stable-python first (RUNBOOK.md)" >&2; exit 1; }
export PATH="$runtime/bin:$PATH"
umask 077
repo=/opt/atlas-src/repo
stable=/opt/atlas-src/stable
previous=/opt/atlas-src/stable-previous
candidate=/opt/atlas-src/stable-candidate
marker=${ATLAS_STABLE_CI_MARKER:-/opt/atlas-src/stable-ci-green-tags}

[[ -f "$marker" ]] || { echo "missing approved CI marker: $marker" >&2; exit 1; }
git -C "$repo" rev-parse --verify --quiet "refs/tags/$tag^{commit}" >/dev/null || {
  echo "tag does not resolve to a commit: $tag" >&2; exit 1;
}
grep -Fx -- "$tag" "$marker" >/dev/null || {
  echo "tag has no owner-approved green CI marker: $tag" >&2; exit 1;
}
[[ ! -e "$candidate" ]] || { echo "candidate worktree already exists" >&2; exit 1; }
git -C "$repo" worktree add --detach "$candidate" "refs/tags/$tag"
cleanup() { git -C "$repo" worktree remove --force "$candidate" 2>/dev/null || true; }
trap cleanup EXIT
(cd "$candidate" && export ATLAS_PORT_BASE=1900 ATLAS_STABLE=0 ATLAS_SEARCH_PROVIDER=serpapi GITHUB_ACTIONS=false && bash scripts/test.sh)
sudo systemctl stop atlas-stable.service
if [[ -e "$previous" ]]; then git -C "$repo" worktree remove --force "$previous"; fi
if [[ -e "$stable" ]]; then git -C "$repo" worktree move "$stable" "$previous"; fi
git -C "$repo" worktree add --detach "$stable" "refs/tags/$tag"
ln -s "$runtime" "$stable/.venv"
sudo systemctl start atlas-stable.service
ready=false
for attempt in $(seq 1 60); do
  if "$stable/infra/stable/healthcheck.sh" --ready; then ready=true; break; fi
  sleep 2
done
if ! "$ready" || ! "$stable/infra/stable/healthcheck.sh"; then
  echo "stable health failed; run the retained rollback before reopening the front door" >&2
  exit 1
fi
printf '%s\n' "$tag" | sudo tee /opt/atlas-src/stable-last-deploy >/dev/null
