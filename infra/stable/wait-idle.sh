#!/usr/bin/env bash
# guest_wait_idle.sh: wait until atlas.eu.com has had no activity for QUIET seconds, so a restart never cuts a user.
# Activity = any POST from the web UI chat or any API key route (/api/atlas/v1/*: chat, responses, messages),
# plus any open orchestrator connection. Owner rule 2026-10-30: never restart with activity in the last 5 min;
# there is no forced deploy: after MAX seconds this exits 1 and the caller stops (ask the owner).
QUIET=${QUIET:-420}; MAX=${MAX:-10800}; start=$(date +%s)
while :; do
  now=$(date +%s)
  last=$(grep -E '"POST /api/(chat/completions|v1/chat/completions|atlas/v1/)' /var/log/nginx/access.log | tail -1 | sed -E 's/.*\[([^]]+)\].*/\1/' | sed -E 's#:# #; s#/# #g')
  lastts=$( [ -n "$last" ] && date -d "$last" +%s 2>/dev/null || echo 0 )
  conns=$(ss -Htn state established "( sport = :8120 or dport = :8120 )" | wc -l)
  if (( now - lastts >= QUIET && conns == 0 )); then echo "$(date -Is) guest idle (last activity $((now - lastts)) s ago)"; exit 0; fi
  if (( now - start >= MAX )); then echo "$(date -Is) guest still busy after $MAX s: NOT deploying, owner decision needed"; exit 1; fi
  sleep 30
done
