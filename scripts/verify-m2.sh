#!/usr/bin/env bash
# verify-m2 — Ingestion + RAG. PROTÉGÉ (CODEOWNERS).
#
# Vérifie que le pipeline RAG fonctionne sur le corpus multilingue (corpus/) :
#   1. l'ingestion produit des chunks avec métadonnées (doc_id, langue, source)
#   2. le retrieval atteint un recall@8 >= SEUIL sur le jeu doré E1
#   3. les citations d'une réponse RAG pointent vers un chunk réellement récupérable
#
# Le pipeline est fourni par l'agent (services/retrieval + ingestion). Ce script
# appelle l'outil d'éval que l'agent expose : `make eval-retrieval` doit produire
# un rapport JSON sous BRAIN/eval/retrieval.json avec un champ "recall_at_8".
#
# Contrairement à M1, M2 ne crée PAS de GPU : il peut tourner sur la VM ET en CI
# (si les embeddings sont calculables sans GPU, ex. via un modèle CPU ou l'API
# d'embeddings Scaleway). Le seuil est volontairement progressif (cf. MISSION.md).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m2: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m2 ==" >&2

# Seuil de recall@8. MISSION.md vise 0.85 en cible ; on démarre le gate à 0.70 pour
# un corpus multilingue difficile (arabe/chinois inclus), à relever ensuite.
SEUIL="${M2_RECALL_MIN:-0.70}"

# 1. le corpus existe
CORPUS_N=$(ls corpus/*.md 2>/dev/null | wc -l)
[[ "$CORPUS_N" -ge 10 ]] || fail "corpus incomplet ($CORPUS_N docs, attendu >= 10)"
pass "corpus présent ($CORPUS_N documents)"

# 2. le jeu doré E1 existe
[[ -f evals/golden/e1_retrieval.yaml ]] || fail "jeu doré E1 manquant"
pass "jeu doré E1 présent"

# 3. l'ingestion a produit des chunks avec métadonnées
#    (l'agent expose `make ingest` qui remplit la base ; on vérifie le résultat)
if ! make ingest >&2 2>/dev/null; then
  fail "make ingest a échoué (l'ingestion du corpus doit produire des chunks)"
fi
pass "ingestion exécutée"

# 4. évaluation du retrieval sur E1 -> recall@8
if ! make eval-retrieval >&2 2>/dev/null; then
  fail "make eval-retrieval a échoué"
fi
REPORT="BRAIN/eval/retrieval.json"
[[ -f "$REPORT" ]] || fail "rapport $REPORT absent après eval-retrieval"

RECALL=$(python3 -c "import json;print(json.load(open('$REPORT')).get('recall_at_8','NA'))" 2>/dev/null || echo NA)
[[ "$RECALL" != "NA" ]] || fail "champ recall_at_8 absent du rapport"

# comparaison numérique
OK=$(python3 -c "print(1 if float('$RECALL') >= float('$SEUIL') else 0)" 2>/dev/null || echo 0)
[[ "$OK" == "1" ]] || fail "recall@8 = $RECALL < seuil $SEUIL (voir $REPORT)"
pass "recall@8 = $RECALL (>= $SEUIL)"

# 5. citations résolvables : le rapport doit attester que chaque citation testée
#    pointe vers un chunk existant (champ "citations_resolues": true)
CIT=$(python3 -c "import json;print(json.load(open('$REPORT')).get('citations_resolues','false'))" 2>/dev/null || echo false)
[[ "$CIT" == "True" || "$CIT" == "true" ]] || fail "citations non résolvables (citations_resolues != true)"
pass "citations résolvables vers des chunks réels"

echo "== verify-m2 OK (ingestion + recall@8 $RECALL + citations résolues) ==" >&2
