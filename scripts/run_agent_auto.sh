#!/usr/bin/env bash
# run_agent_auto.sh — AUTONOMOUS NIGHT mode: run the supplied milestones without intervention.
# The agent resolves MINOR contradictions (thresholds/case counts/relaxations
# already documented for the PoC) and CONTINUES; it stops ONLY for SECURITY contradictions
# (tenant isolation, secrets, budgets, destructive actions) or an actual technical failure.
# Usage : run_agent_auto.sh M5 M6
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; [[ -f .env ]] && source .env; set +a

mkdir -p BRAIN
for MILESTONE in "$@"; do
  echo "=== $(date -Is) — AUTO: starting $MILESTONE ===" | tee -a BRAIN/agent.log

  read -r -d '' PROMPT <<EOF || true
Implement milestone ${MILESTONE} of the ATLAS-0 project, in AUTONOMOUS MODE.

Before taking any action: read AGENTS.md, MISSION.md, docs/13-poc-spec.md. Follow docs/11 and docs/14.

AUTONOMY RULE (night): for any MINOR contradiction between MISSION.md and
docs/13 involving a THRESHOLD, CASE COUNT, LANGUAGE DISTRIBUTION, or a
RELAXATION already marked "PoC" in either document, DO NOT stop:
apply the LESS STRICT version (the one matching the actual state of the
delivered golden sets), document this choice in the report and BRAIN/JOURNAL.md using
the marker "CONTRADICTION résolue en autonomie:", and CONTINUE.

Stop and write to BRAIN/BLOCKERS.md ONLY for:
- SECURITY contradiction or risk (tenant isolation, leaked secret, exceeded budget,
  destructive action, paid call outside the budget);
- actual TECHNICAL failure after 3 attempts (broken dependency, persistent provider error);
- a protected file requiring modification (CODEOWNERS/verify-*/workflows).

Definition of done for ${MILESTONE} :
  1. make verify-${MILESTONE,,} passes LOCALLY, then
  2. branch ${MILESTONE,,}-<topic>, commit, push, PR, then
  3. wait for GREEN CI 'ci' (poll GitHub API, max 180 attempts × 15s = 45 min (this repo's CI is slow, be patient)), then
  4. if CI is green, MERGE yourself via the GitHub API (squash), then update BRAIN/.

MERGE AUTHORIZATION: allowed if and only if the 'ci' job is green. Never modify
a protected file. Paid calls (Scaleway/SerpApi) are authorized within the
per-request budget; a GPU is NOT required for M5/M6.

Once ${MILESTONE} is completed and merged, AUTOMATICALLY proceed to the next milestone
if one remains in the list. Write "JALON ${MILESTONE} TERMINÉ" in BRAIN/JOURNAL.md.
EOF

  set +e
  codex exec "$PROMPT" \
    --dangerously-bypass-approvals-and-sandbox \
    --json \
    --output-last-message "BRAIN/${MILESTONE}.last.txt" \
    >> "BRAIN/${MILESTONE}.jsonl" 2>> BRAIN/agent.log
  CODE=$?
  set -e

  echo "=== $(date -Is) — AUTO: $MILESTONE finished (exit $CODE) ===" | tee -a BRAIN/agent.log

  # If the milestone reports a security blocker, stop the entire sequence
  if [[ -f "BRAIN/${MILESTONE}.done" ]]; then
    echo "AUTO: $MILESTONE marked done, continuing." | tee -a BRAIN/agent.log
  fi
  # Detect an explicit blocker: if BLOCKERS.md was changed recently AND no merge occurred,
  # stop to avoid starting the next milestone from a broken state.
  if grep -q "ARRÊT SÉCURITÉ\|SECURITY STOP" BRAIN/BLOCKERS.md 2>/dev/null; then
    echo "AUTO: security stop detected, sequence interrupted." | tee -a BRAIN/agent.log
    break
  fi
done
echo "=== $(date -Is) — AUTO: sequence finished ===" | tee -a BRAIN/agent.log
