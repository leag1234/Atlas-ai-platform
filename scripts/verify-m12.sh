#!/usr/bin/env bash
# verify-m12 — description d'image (multimodal entrée, pixtral). PROTÉGÉ.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m12: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m12 ==" >&2

grep -rqiE "pixtral|vision|image" services/model-gateway/ services/orchestrator/ 2>/dev/null || fail "routage vision/image absent"
pass "routage vision présent"

if [[ -z "${SCW_GENERATIVE_API_KEY:-}" ]]; then
  pass "contrôle statique OK (test réel = avec accès Scaleway)"; echo "== verify-m12 OK (statique) ==" >&2; exit 0
fi
if ! make test-vision >&2 2>/dev/null; then
  fail "make test-vision échoue (description d'une image de test)"
fi
R="BRAIN/eval/vision.json"; [[ -f "$R" ]] || fail "rapport vision absent"
OK=$(python3 -c "import json;print(json.load(open('$R')).get('described',False))" 2>/dev/null||echo False)
[[ "$OK" == "True" ]] || fail "l'image de test n'a pas été décrite correctement"
pass "image décrite (multimodal entrée fonctionnel)"
echo "== verify-m12 OK ==" >&2
