#!/usr/bin/env bash
# Download the pinned weights of weights.json into DEST (default
# /opt/pictures-cpu/weights), check size and sha256 of each, then convert the
# 4B model to 4 bits with the engine image's converter and check the result.
# Data files only (.gguf, .safetensors, .json); nothing is executed from them.
# Usage: fetch_weights.sh [DEST] [--without-fallback]   (run as root on the image server)
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${1:-/opt/pictures-cpu/weights}"
fallback=1
[[ "${2:-}" == --without-fallback ]] && fallback=0
image="${PICTURES_CPU_IMAGE:-atlas-pictures-cpu:local}"
mkdir -p "$dest/filter"
rows="$(python3 - "$here/weights.json" "$fallback" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1]))
for f in spec["files"]:
    if f.get("fallback") and sys.argv[2] != "1":
        continue
    print(f["name"], f["repo"], f["rev"], f["file"], f["bytes"], f["sha256"], int(bool(f.get("transient"))))
PY
)"
check() {  # path bytes sha256
  [[ -f "$1" ]] && [[ "$(stat -c %s "$1")" == "$2" ]] && [[ "$(sha256sum "$1" | cut -d' ' -f1)" == "$3" ]]
}
converted="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["converted"][0]; print(c["name"], c["from"], c["type"], c["bytes"], c["sha256"])' "$here/weights.json")"
read -r cname cfrom ctype cbytes csha <<<"$converted"
while read -r name repo rev file bytes sha transient; do
  case "$name" in *.gguf|*.safetensors|*.json) ;; *) echo "refused: $name"; exit 1;; esac
  out="$dest/$name"
  if [[ "$transient" == 1 ]] && check "$dest/$cname" "$cbytes" "$csha"; then
    echo "have $cname (skip $name)"; continue
  fi
  if check "$out" "$bytes" "$sha"; then echo "have $name"; continue; fi
  curl -sfL --retry 3 --proto '=https' -o "$out.part" "https://huggingface.co/$repo/resolve/$rev/$file"
  mv "$out.part" "$out"
  check "$out" "$bytes" "$sha" || { echo "sha256 mismatch: $name"; rm -f "$out"; exit 1; }
  echo "ok $name"
done <<<"$rows"
if ! check "$dest/$cname" "$cbytes" "$csha"; then
  docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --user 0:0 -v "$dest:/w" --entrypoint /opt/sd/sd-cli \
    "$image" -M convert -m "/w/$cfrom" -o "/w/$cname.part" --type "$ctype" >/dev/null
  mv "$dest/$cname.part" "$dest/$cname"
  check "$dest/$cname" "$cbytes" "$csha" || { echo "sha256 mismatch: $cname"; exit 1; }
  echo "ok $cname"
fi
rm -f "$dest/$cfrom"
chmod -R a+rX "$dest"
du -sh "$dest"
