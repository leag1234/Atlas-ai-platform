#!/bin/sh
# Atlas: connect this computer to your Atlas conversations.
#
# Atlas then runs commands in the folder you connect from, as you would in this
# terminal (build, tests, git with your own credentials, ssh to your own
# servers), and asks you first in the conversation before anything that leaves
# that folder or cannot be undone. Nothing listens on this computer: upterm
# (Apache-2.0, a pinned and checksummed release) dials out to the Atlas relay,
# and only this computer's Atlas key may come in through it.
#
#   sh ~/.atlas/connect.sh ATLAS_URL TOKEN   first time, from your project folder
#   sh ~/.atlas/connect.sh                   connect again
#   sh ~/.atlas/connect.sh --persistent      stay connected in the background
#   sh ~/.atlas/connect.sh --stop            end a background connection
#
# Ctrl-C ends the access. "Forget my computer" in Atlas removes it for good.
# Everything Atlas keeps here is in ~/.atlas.
set -eu
umask 077

DIR="$HOME/.atlas"
VERSION=0.41.1
SUM_darwin_amd64=fc4b80d3f57b42b4addf5c4b182ea16334d6e1196e988b9ea0ff69a123709b17
SUM_darwin_arm64=47076b7012ec914d403e6c8d91de80732ac8d6dba5e8d62439f1bfcce5f15c86
SUM_linux_amd64=3007d4ce09e9f70a390e1b52e5a0b591fc9fa473196ca76d8f72b5f1cd723b31
SUM_linux_arm64=25353fd52538acb9b2a5c95f2d9d3caa6df14f50413c2a56f33cded83b9bc1e9
UPTERM="$DIR/bin/upterm-$VERSION"
LABEL=org.atlas.connect

say() { printf '%s\n' "$*"; }
fail() { printf 'Atlas: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is required: $2"; }

# Read one field of a JSON file (python3 is also what Atlas uses to run commands).
field() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }

platform() {
  case "$(uname -s)" in
    Darwin) OS=macos; GOOS=darwin ;;
    Linux) OS=linux; GOOS=linux ;;
    *) fail "this system is not supported (macOS and Linux are)." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) GOARCH=amd64 ;;
    arm64|aarch64) GOARCH=arm64 ;;
    *) fail "this processor is not supported (x86_64 and arm64 are)." ;;
  esac
}

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}

install_upterm() {
  [ -x "$UPTERM" ] && return 0
  base=$(field "$DIR/machine.json" upterm_download)
  eval "expected=\${SUM_${GOOS}_${GOARCH}}"
  work=$(mktemp -d)
  archive="$work/upterm.tar.gz"
  say "Downloading upterm $VERSION ($GOOS/$GOARCH)..."
  curl -fsSL "$base/upterm_${GOOS}_${GOARCH}.tar.gz" -o "$archive" || fail "upterm could not be downloaded."
  [ "$(sha256 "$archive")" = "$expected" ] || { rm -rf "$work"; fail "the upterm download does not match its checksum; nothing was installed."; }
  tar -xzf "$archive" -C "$work" upterm
  mkdir -p "$DIR/bin"
  mv "$work/upterm" "$UPTERM"
  chmod 755 "$UPTERM"
  rm -rf "$work"
}

enrol() {
  origin=$1 token=$2
  folder=$(pwd -P)
  [ "$folder" != "/" ] && [ "$folder" != "$(cd "$HOME" && pwd -P)" ] || fail "go to the folder of your project first (cd ~/code/my-project), then run this command again: Atlas works in the folder you connect from."
  mkdir -p "$DIR" && chmod 700 "$DIR"
  [ -f "$DIR/host_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C atlas-host -f "$DIR/host_ed25519"
  python3 - "$token" "$(cat "$DIR/host_ed25519.pub")" "$(id -un)" "$OS" "$(uname -m)" "$folder" > "$DIR/enrol.json" <<'PY'
import json, sys
token, key, user, osname, arch, folder = sys.argv[1:]
print(json.dumps(dict(token=token, host_key=key, user=user, os=osname, arch=arch, work_root=folder)))
PY
  code=$(curl -sS -o "$DIR/machine.json.new" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary "@$DIR/enrol.json" "$origin/api/atlas/connect/enrol") || code=000
  rm -f "$DIR/enrol.json"
  if [ "$code" != 200 ]; then
    error=$(field "$DIR/machine.json.new" error 2>/dev/null || echo "status $code")
    rm -f "$DIR/machine.json.new"
    case "$error" in
      connect_token_expired_or_used) fail "this command was already used or is more than 15 minutes old: ask Atlas for a new one." ;;
      *) fail "Atlas refused the connection ($error)." ;;
    esac
  fi
  mv "$DIR/machine.json.new" "$DIR/machine.json"
  python3 - "$DIR/machine.json" "$origin" "$folder" <<'PY'
import json, sys
path, origin, folder = sys.argv[1:]
data = json.load(open(path))
data.update(origin=origin, folder=folder)
json.dump(data, open(path, "w"))
open(path.rsplit("/", 1)[0] + "/atlas_guest.pub", "w").write(data["guest_key"] + "\n")
open(path.rsplit("/", 1)[0] + "/known_hosts", "w").write(data["relay_known_host"] + "\n")
PY
}

announce() {
  user=$1
  msg="$DIR/announce.txt"
  printf 'atlas-connect-v1\n%s\n%s\n%s\n' "$(field "$DIR/machine.json" machine_id)" "$user" "$(date +%s)" > "$msg"
  sig=$(ssh-keygen -Y sign -f "$DIR/host_ed25519" -n atlas-connect < "$msg" 2>/dev/null) || fail "this computer's Atlas key could not sign."
  python3 - "$DIR/machine.json" "$msg" "$sig" > "$DIR/announce.json" <<'PY'
import json, sys
path, msg, sig = sys.argv[1:]
print(json.dumps(dict(machine=json.load(open(path))["machine"], message=open(msg).read(), signature=sig)))
PY
  code=$(curl -sS -o "$DIR/announce.reply" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary "@$DIR/announce.json" "$(field "$DIR/machine.json" origin)/api/atlas/connect/announce") || code=000
  rm -f "$DIR/announce.json" "$msg"
  [ "$code" = 200 ]
}

NAME=atlas-connection

stop_session() {
  "$UPTERM" session stop "$NAME" >/dev/null 2>&1 || true
  announce - || true
}

start_session() {
  "$UPTERM" session stop "$NAME" >/dev/null 2>&1 || true
  tries=0
  # Right after a first connection the relay reloads its list of computers:
  # a few seconds of refusals are expected.
  until "$UPTERM" host --server "$(field "$DIR/machine.json" relay_url)" \
      --private-key "$DIR/host_ed25519" --known-hosts "$DIR/known_hosts" \
      --authorized-keys "$DIR/atlas_guest.pub" \
      --allow-exec --allow-sftp --no-forwarding --accept --detach -o json \
      --name "$NAME" -- sh -c 'exec tail -f /dev/null' > "$DIR/session.json" 2> "$DIR/session.err"; do
    tries=$((tries + 1))
    [ "$tries" -lt 10 ] || fail "the Atlas relay could not be reached ($(tail -n 1 "$DIR/session.err"))."
    sleep 3
  done
  user=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["sshConfig"]; print([l.split()[1] for l in c.splitlines() if l.strip().startswith("User ")][0])' "$DIR/session.json")
  announce "$user" || { "$UPTERM" session stop "$NAME" >/dev/null 2>&1 || true; fail "Atlas did not accept this computer ($(field "$DIR/announce.reply" error))."; }
}

service_file() {
  if [ "$OS" = macos ]; then
    printf '%s\n' "$HOME/Library/LaunchAgents/$LABEL.plist"
  else
    printf '%s\n' "$HOME/.config/systemd/user/atlas-connect.service"
  fi
}

persistent() {
  file=$(service_file)
  mkdir -p "$(dirname "$file")"
  if [ "$OS" = macos ]; then
    cat > "$file" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/sh</string><string>$DIR/connect.sh</string><string>--service</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>$DIR/service.log</string>
</dict></plist>
EOF
    [ "${ATLAS_CONNECT_DRY_RUN:-}" ] || launchctl bootstrap "gui/$(id -u)" "$file"
  else
    cat > "$file" <<EOF
[Unit]
Description=Atlas connection of this computer
[Service]
ExecStart=/bin/sh $DIR/connect.sh --service
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
EOF
    [ "${ATLAS_CONNECT_DRY_RUN:-}" ] || { systemctl --user daemon-reload && systemctl --user enable --now atlas-connect.service; }
  fi
  say "Atlas stays connected to $(field "$DIR/machine.json" folder) in the background, also after a restart. End it with: sh ~/.atlas/connect.sh --stop"
}

stop_persistent() {
  file=$(service_file)
  if [ -f "$file" ]; then
    if [ "$OS" = macos ]; then launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    else systemctl --user disable --now atlas-connect.service 2>/dev/null || true; fi
    rm -f "$file"
  fi
  if [ -x "$UPTERM" ] && [ -f "$DIR/machine.json" ]; then stop_session; fi
  say "Atlas no longer has access to this computer."
}

platform
need python3 "install the developer tools (macOS: xcode-select --install)."
need ssh-keygen "install OpenSSH."
need curl "install curl."
need tar "install tar."

case "${1:-}" in
  --stop) stop_persistent; exit 0 ;;
  --persistent) [ -f "$DIR/machine.json" ] || fail "connect once first."; install_upterm; persistent; exit 0 ;;
  --service)
    [ -f "$DIR/machine.json" ] || fail "connect once first."
    install_upterm; start_session
    trap 'stop_session; exit 0' INT TERM HUP
    "$UPTERM" session wait "$NAME" >/dev/null 2>&1 || true
    stop_session; exit 0 ;;
  http://*|https://*)
    [ $# -eq 2 ] || fail "usage: sh ~/.atlas/connect.sh ATLAS_URL TOKEN"
    enrol "$1" "$2" ;;
  "") [ -f "$DIR/machine.json" ] || fail "ask Atlas for the connection command first." ;;
  *) fail "unknown option $1" ;;
esac

install_upterm
start_session
trap 'stop_session; say "Atlas no longer has access to this computer."; exit 0' INT TERM HUP
say "Atlas is connected to $(field "$DIR/machine.json" folder)."
say "Leave this window open while Atlas works. Press Ctrl-C to end the access."
"$UPTERM" session wait "$NAME" >/dev/null 2>&1 || true
stop_session
say "The connection ended."
