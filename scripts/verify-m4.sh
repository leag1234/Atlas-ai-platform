#!/usr/bin/env bash
# verify-m4 — Cascade de routage + fallback. PROTÉGÉ (CODEOWNERS).
#
# Coeur de M4 : le routage par classe (simple->local, complexe->escalade) et la
# résilience (si le modèle local est injoignable, le gateway bascule sur l'escalade
# Scaleway sans échec utilisateur). L'UI (Open WebUI/LibreChat) est un PLUS visuel,
# NON bloquant pour ce gate PoC.
#
# La panne GPU est simulée en rendant l'endpoint LOCAL injoignable (pas besoin de
# créer un vrai GPU payant) : le fallback doit prendre le relais.
#
# Critères (MISSION.md M4) :
#   - POC-E8 (routage) >= 85% ; zéro sous-routage sur les cas 'critique'
#   - fallback : endpoint local coupé -> le gateway répond quand même via escalade
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m4: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m4 ==" >&2

# 1. le classifieur de routage existe
grep -rqiE "route|routing|classif" services/model-gateway/ 2>/dev/null || fail "logique de routage absente du gateway"
pass "logique de routage présente"

# 2. jeu doré E8 présent
[[ -f evals/golden/e8_routage.yaml ]] || fail "jeu doré E8 manquant"
pass "jeu doré E8 présent"

# 3. évaluation du routage E8 -> matrice de confusion
if ! make eval-routing >&2 2>/dev/null; then
  fail "make eval-routing a échoué"
fi
REPORT="BRAIN/eval/routing.json"
[[ -f "$REPORT" ]] || fail "rapport $REPORT absent"

ACC=$(python3 -c "import json;print(json.load(open('$REPORT')).get('accuracy','NA'))" 2>/dev/null || echo NA)
[[ "$ACC" != "NA" ]] || fail "champ accuracy absent du rapport"
OK=$(python3 -c "print(1 if float('$ACC') >= 0.85 else 0)" 2>/dev/null || echo 0)
[[ "$OK" == "1" ]] || fail "routage accuracy = $ACC < 0.85"
pass "routage E8 = $ACC (>= 0.85)"

# 4. zéro sous-routage sur les cas critiques (complexe classé simple = grave)
SOUS=$(python3 -c "import json;print(json.load(open('$REPORT')).get('sous_routage_critique','NA'))" 2>/dev/null || echo NA)
[[ "$SOUS" == "0" ]] || fail "sous-routage sur cas critique(s) : $SOUS (doit être 0)"
pass "zéro sous-routage sur les cas critiques"

# 5. fallback : endpoint local injoignable -> le gateway répond via escalade
if ! make test-fallback >&2 2>/dev/null; then
  fail "test de fallback échoue (le gateway doit basculer sur l'escalade si le local est coupé)"
fi
pass "fallback OK (bascule sur escalade quand le local est injoignable)"

# 6. UI : présence détectée = bonus informatif, non bloquant
if grep -rqiE "open.?webui|librechat|ui" services/ docker-compose*.yml 2>/dev/null; then
  pass "UI configurée (bonus)"
else
  echo "  (UI non détectée — non bloquant pour le gate M4 PoC)" >&2
fi

echo "== verify-m4 OK (routage E8=$ACC, zéro sous-routage critique, fallback) ==" >&2
