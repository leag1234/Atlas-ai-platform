#!/usr/bin/env bash
# verify-m13 — génération d'image locale sur GPU (Flux), souverain. PROTÉGÉ.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m13: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m13 ==" >&2

# service de génération d'image présent, local, aucun appel externe
grep -rqiE "flux|diffus|image.?gen|text.?to.?image" services/ infra/ 2>/dev/null || fail "service de génération d'image absent"
pass "service de génération d'image présent"

# garde-fou souveraineté : aucun endpoint externe de génération d'image
if grep -rniE "openai\.com/v1/images|stability\.ai|replicate\.com|api\.midjourney" services/ 2>/dev/null; then
  fail "appel à un service de génération d'image EXTERNE détecté (doit être 100% local)"
fi
pass "aucun service externe de génération d'image (souveraineté OK)"

# CI / sans GPU : statique
if [[ -z "${SCW_ACCESS_KEY:-}" ]]; then
  pass "contrôle statique OK (test réel = sur la VM avec GPU)"; echo "== verify-m13 OK (statique) ==" >&2; exit 0
fi

# test réel : génère une image, servie localement
if ! make test-imagegen >&2 2>/dev/null; then
  fail "make test-imagegen échoue"
fi
R="BRAIN/eval/imagegen.json"; [[ -f "$R" ]] || fail "rapport imagegen absent"
OK=$(python3 -c "import json;d=json.load(open('$R'));print(d.get('image_produced') and d.get('served_locally'))" 2>/dev/null||echo False)
[[ "$OK" == "True" ]] || fail "image non produite ou non servie localement"
pass "image générée localement sur GPU (souverain)"
echo "== verify-m13 OK ==" >&2
