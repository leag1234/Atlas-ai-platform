#!/usr/bin/env bash
# Publish only the static Atlas front-door files. It never deletes remote content.
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dry_run=false
if [[ "${1:-}" == "--dry-run" ]]; then
    dry_run=true
    shift
fi
if (( $# )); then
    echo "usage: $0 [--dry-run]" >&2
    exit 2
fi

args=(-az --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r)
if "$dry_run"; then
    args+=(--dry-run)
fi

rsync "${args[@]}" "$root/static/" gael@indidea.org:/var/www/indidea/atlas/
