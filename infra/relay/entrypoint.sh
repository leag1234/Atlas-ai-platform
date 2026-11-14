#!/bin/sh
# Run uptermd and restart it when the gate (the host keys of enrolled
# computers, written by the Atlas runner) changes: uptermd reads it once at
# start. Connected computers register again under the same session.
set -eu
KEY=/keys/relay_ed25519
GATE=/gate/hosts
EMPTY=/tmp/no-hosts
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C atlas-relay -f "$KEY"
: > "$EMPTY"
# uptermd logs a connecting computer's local account name (as "host", and as
# the "session" of its HOST connection): personal data the relay has no use
# for. Its log goes through a filter that blanks it.
LOG=/tmp/uptermd.log
[ -p "$LOG" ] || mkfifo "$LOG"
awk '{
  gsub(/"host":"[^"]*"/, "\"host\":\"-\"")
  if ($0 ~ /"type":"HOST"/) gsub(/"session":"[^"]*"/, "\"session\":\"-\"")
  print; fflush()
}' < "$LOG" &
exec 3> "$LOG"
pid=""
trap '[ -n "$pid" ] && kill "$pid" 2>/dev/null; exit 0' TERM INT
state() { if [ -f "$GATE" ]; then sha256sum "$GATE" | cut -d" " -f1; else echo none; fi; }
while :; do
  seen=$(state)
  list=$EMPTY
  [ -f "$GATE" ] && list=$GATE
  uptermd --ssh-addr 0.0.0.0:2222 --ws-addr 0.0.0.0:8080 \
    --private-key "$KEY" --authorized-keys "$list" >&3 2>&3 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2
    if [ "$(state)" != "$seen" ]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      break
    fi
  done
  wait "$pid" 2>/dev/null || sleep 1
done
