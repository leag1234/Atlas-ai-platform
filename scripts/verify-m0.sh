#!/usr/bin/env bash
# verify-m0 — cadre vérifiable. PROTÉGÉ (CODEOWNERS) : l'agent ne modifie pas ce fichier.
# Un jalon est "fait" quand ce script sort 0 EN CI.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m0: $*"; exit 1; }
pass(){ echo "  ✓ $*"; }

echo "== verify-m0 =="

make lint      >/dev/null || fail "lint échoue"
pass "lint"
make typecheck >/dev/null || fail "typecheck échoue"
pass "typecheck"
make test      >/dev/null || fail "tests échouent"
pass "tests"
make scan-secrets >/dev/null || fail "scan de secrets échoue"
pass "scan-secrets"

# eval-harness tourne à vide sans crasher
make eval-smoke >/dev/null || fail "eval-smoke ne s'exécute pas"
pass "eval-smoke exécutable"

# healthcheck edge-bff (démarré par le test d'intégration ou docker compose)
if [[ -f services/edge-bff/healthcheck.sh ]]; then
  bash services/edge-bff/healthcheck.sh || fail "edge-bff ne répond pas 200 sur /health"
  pass "edge-bff /health"
else
  fail "services/edge-bff/healthcheck.sh manquant (livrable M0)"
fi

# anti nom de modèle en dur (doublon local du gate CI, pour exécution locale)
if grep -rniE 'qwen|glm|deepseek|llama|mistral|kimi' \
     --include='*.py' --include='*.ts' --include='*.go' \
     --exclude-dir=services/model-gateway services/ 2>/dev/null; then
  fail "nom de modèle en dur hors model-gateway"
fi
pass "aucun nom de modèle en dur hors gateway"

echo "== verify-m0 OK =="
