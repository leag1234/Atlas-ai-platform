#!/usr/bin/env bash
# verify-m11 — streaming SSE + réflexion visible. PROTÉGÉ.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m11: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m11 ==" >&2

grep -rqiE "stream|sse|text/event-stream" services/ 2>/dev/null || fail "streaming non implémenté dans l'adaptateur"
pass "streaming déclaré dans l'adaptateur"

if ! make test-streaming >&2 2>/dev/null; then
  fail "make test-streaming échoue"
fi
R="BRAIN/eval/streaming.json"; [[ -f "$R" ]] || fail "rapport streaming absent"
CHUNKS=$(python3 -c "import json;print(json.load(open('$R')).get('chunk_count',0))" 2>/dev/null||echo 0)
[[ "$CHUNKS" -ge 2 ]] || fail "la réponse n'est pas streamée (chunk_count=$CHUNKS, attendu >=2)"
pass "réponse streamée en $CHUNKS fragments"
echo "== verify-m11 OK ==" >&2
