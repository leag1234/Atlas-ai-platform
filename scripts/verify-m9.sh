#!/usr/bin/env bash
# verify-m9 — mémoire + projets (modèle Claude/ChatGPT). PROTÉGÉ.
# Vérifie : notion de projet (contexte partagé), mémoire de faits extraits/éditables,
# réinjection pertinente, isolation entre projets, effacement effectif.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail(){ echo "::error::verify-m9: $*" >&2; exit 1; }
pass(){ echo "  ✓ $*" >&2; }
echo "== verify-m9 ==" >&2

# composants présents
grep -rqiE "project|projet" services/ 2>/dev/null || fail "notion de projet absente"
grep -rqiE "memory|memoire|fact" services/ 2>/dev/null || fail "mémoire de faits absente"
pass "composants projet + mémoire présents"

# suite de tests d'intégration mémoire
if ! make test-memory >&2 2>/dev/null; then
  fail "make test-memory échoue"
fi
R="BRAIN/eval/memory.json"; [[ -f "$R" ]] || fail "rapport mémoire absent"

# 1. un fait donné dans une conversation est réutilisé dans une autre du MÊME projet
CROSS=$(python3 -c "import json;print(json.load(open('$R')).get('fact_reused_across_conversations',False))" 2>/dev/null||echo False)
[[ "$CROSS" == "True" ]] || fail "un fait n'est pas réutilisé entre conversations d'un même projet"
pass "mémoire partagée entre conversations d'un projet"

# 2. isolation : pas de fuite de contexte entre projets différents
ISO=$(python3 -c "import json;print(json.load(open('$R')).get('projects_isolated',False))" 2>/dev/null||echo False)
[[ "$ISO" == "True" ]] || fail "fuite de contexte entre projets (isolation non respectée)"
pass "isolation entre projets respectée"

# 3. effacement de la mémoire effectif
ERASE=$(python3 -c "import json;print(json.load(open('$R')).get('erasure_effective',False))" 2>/dev/null||echo False)
[[ "$ERASE" == "True" ]] || fail "l'effacement de la mémoire n'est pas effectif"
pass "effacement de la mémoire effectif"

# 4. distinction mémoire vs source dans la réponse
DIST=$(python3 -c "import json;print(json.load(open('$R')).get('memory_vs_source_distinguished',False))" 2>/dev/null||echo False)
[[ "$DIST" == "True" ]] || fail "le système ne distingue pas mémoire et source"
pass "distinction mémoire/source présente"

echo "== verify-m9 OK ==" >&2
