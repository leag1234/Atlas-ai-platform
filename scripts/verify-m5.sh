#!/usr/bin/env bash
# verify-m5 — Évals complètes + télémétrie. PROTÉGÉ (CODEOWNERS).
#
# M5 consolide toutes les suites d'éval (E1..E9, déjà validées) en une exécution
# unifiée `make eval`, produit un rapport avec ventilation par langue, expose la
# télémétrie (tokens, coût, latence, cache hit), et calcule une calibration CROISÉE
# du juge : accord entre le juge de production (glm-5.2) et un juge de référence
# d'une AUTRE famille (Claude), via BRAIN/eval/calibration.json.
# NB : ceci est une calibration inter-modèles de référence, PAS une calibration
# humaine (celle-ci reste une action pré-GA). Le rapport doit le nommer ainsi.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m5: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m5 ==" >&2

# Repartir propre : supprimer les rapports d'un run précédent pour ne pas les confondre
rm -f BRAIN/eval/report.json BRAIN/eval/telemetry.json BRAIN/eval/calibration.json

# 1. make eval existe et tourne complet (toutes suites) sous le temps imparti
START=$(date +%s)
if ! timeout 1500 make eval >&2 2>/dev/null; then
  fail "make eval a échoué ou dépassé 25 min"
fi
ELAPSED=$(( $(date +%s) - START ))
[[ "$ELAPSED" -lt 1500 ]] || fail "make eval trop long : ${ELAPSED}s"
pass "make eval complet exécuté (${ELAPSED}s)"

# 2. rapport généré avec ventilation par langue
REPORT="BRAIN/eval/report.json"
[[ -f "$REPORT" ]] || fail "rapport $REPORT absent"
HASLANG=$(python3 -c "import json;d=json.load(open('$REPORT'));print(1 if d.get('par_langue') else 0)" 2>/dev/null || echo 0)
[[ "$HASLANG" == "1" ]] || fail "ventilation par langue absente du rapport"
pass "rapport généré avec ventilation par langue"

# 3. suites clés présentes dans le rapport avec un score
for suite in e1 e2 e3 e4 e8; do
  HAS=$(python3 -c "import json;d=json.load(open('$REPORT'));print(1 if '$suite' in {k.lower() for k in d.get('suites',{})} else 0)" 2>/dev/null || echo 0)
  [[ "$HAS" == "1" ]] || fail "suite $suite absente du rapport d'éval"
done
pass "suites E1/E2/E3/E4/E8 présentes dans le rapport"

# 4. télémétrie : tokens, coût, latence, cache hit exposés
TELE="BRAIN/eval/telemetry.json"
[[ -f "$TELE" ]] || fail "télémétrie $TELE absente"
for champ in tokens cost latency cache_hit_ratio; do
  HAS=$(python3 -c "import json;print(1 if '$champ' in json.load(open('$TELE')) else 0)" 2>/dev/null || echo 0)
  [[ "$HAS" == "1" ]] || fail "métrique '$champ' absente de la télémétrie"
done
pass "télémétrie présente (tokens, coût, latence, cache hit)"

# 5. calibration croisée du juge : κ (accord glm-5.2 vs juge de référence)
CAL="BRAIN/eval/calibration.json"
[[ -f "$CAL" ]] || fail "calibration $CAL absente (juge non calibré)"
KAPPA=$(python3 -c "import json;print(json.load(open('$CAL')).get('kappa','NA'))" 2>/dev/null || echo NA)
[[ "$KAPPA" != "NA" ]] || fail "champ kappa absent de la calibration"
# gate souple : le mécanisme doit produire un kappa numérique ; seuil informatif 0.6
TYPE=$(python3 -c "import json;print(json.load(open('$CAL')).get('type',''))" 2>/dev/null || echo "")
pass "calibration du juge présente (kappa=$KAPPA, type=$TYPE)"
# on n'échoue PAS sur un kappa bas (informatif), mais on exige qu'il soit calculé
python3 -c "float('$KAPPA')" 2>/dev/null || fail "kappa non numérique"
pass "kappa calculé et numérique"

echo "== verify-m5 OK (eval complet, rapport par langue, télémétrie, calibration kappa=$KAPPA) ==" >&2
