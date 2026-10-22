# 14 — Autonomous Implementation by AI Agent ("the truck keys")

> Objective: that the PoC (`13`) builds itself with minimal human intervention,
> by a coding agent (Codex, OpenAI) having real access to the infrastructure.
> This document explains how to give the keys **without giving the whole truck**, and how
> to structure the work so that autonomy actually works — because autonomy does not
> come from trust, it comes from **verifiability**.

## 1. The principle governing everything

An agent must never be its own judge. The loop that makes autonomy reliable:

```
agent → commit → CI (tests + evals + lint) → green? → next milestone
                          └── red → agent fixes (it sees CI logs)
```

**The human does not monitor the work; they define milestones and validate at checkpoints.**
Everything below serves to make this loop enforceable: if a success criterion is not
executable by a machine, the agent cannot know it is finished, and autonomy
collapses into back-and-forth exchanges.

## 2. Access scope (give the keys, not the safe)

| Access | Exact scope | Forbidden |
|---|---|---|
| Cloud (Scaleway…) | **A dedicated PoC project**, IAM API key limited to this project: create/destroy GPU instances and volumes, read billing | Access to the organization, other projects, payment methods |
| Budget | Alert at 50/80%, project ceiling if provider allows | — |
| Servers | SSH key dedicated to the agent (revocable independently), `agent` user with sudo on the app VM and GPU node | Any other machine |
| Git | Dedicated PoC repository, agent pushes branches and opens PRs; `main` protected by CI | Direct push to `main` without green CI |
| Serverless provider | API key with **spending cap** at account level | Key without cap |
| Secrets | Injected as CI environment variables + encrypted `.env` (sops/age); never in code nor agent context when avoidable | Plaintext secrets in repo or prompts |

- AUTO-1 (MUST): all keys are created **for** the agent and revocable in one gesture.
  This is your kill switch: revoking 3 keys stops everything, cleanly.
- AUTO-2 (MUST): the cloud account has a budget alert independent of anything the
  agent controls. The agent cannot disable the alarm.
- AUTO-3 (SHOULD): GPU shutdown cron (POC-A2) set **by you** at the provider level
  (instance scheduler), not by the agent — the bill remains bounded even if
  the agent leaves everything running.

## 3. Preparing the mission (this is 80% of success)

The agent rarely fails due to lack of capability; it fails due to ambiguous specification.
Before launching it, the repository contains:

```
/docs/                    ← the corpus, including 13-poc-spec.md
AGENTS.md                 ← permanent agent instructions (see §3.1)
MISSION.md                ← milestones M0→M6 with their verification commands
Makefile                  ← make dev / test / eval-smoke / eval / verify-mN / demo
.github/workflows/ci.yml  ← the CI, defined BEFORE any application code
contracts/                ← internal API schemas for the PoC (even minimal)
evals/golden/             ← the initial golden set (see §5 — your only true contribution)
```

### 3.1 AGENTS.md — mandatory content
Operational summary of doc `11`, adapted for the PoC:
- read `MISSION.md` and `docs/13-poc-spec.md` before any action; in case of conflict,
  `13` prevails;
- a milestone is complete only if `make verify-mN` passes **on CI**, not just locally;
  never modify a verification criterion to make it pass — if a criterion seems wrong,
  write it in `BLOCKERS.md` and move on;
- structured session state (inspired by Antigravity, Apache-2.0, attribution in NOTICE):
  `JOURNAL.md` (narrative: done / decided / blocked), `STATUS.md` (current factual state:
  active cloud resources, current milestone, last CI run), `TASK.md` (current task and
  its next concrete step). Updated at end of session AND before any risky operation —
  this is the cold-start protocol for the next session;
- **mandatory observability markers**, greppable in transcripts and journal: `CONTRADICTION:`
  (docs/code conflict discovered along the way), `RISK:` (request or system state presents
  a danger), `NOTICED BUT NOT TOUCHING:` (defect spotted out of scope — noted, not fixed,
  cf. R-08), `ASSUMPTION:` (hypothesis made due to lack of information — never silent),
  `Source:` (introduction of an API/command not yet used, with its origin).
  Compliance audit at checkpoints = grep for these markers;
- **re-anchoring**: at the start of each task and after any context compaction,
  re-read the "Absolute Rules" section of `docs/11` and `TASK.md` before continuing —
  rules loaded at session start degrade as context fills;
  if the human has to remind you of a rule, it is already a failure to record;
- costs: before creating a cloud resource, write its estimated cost/hour in the journal;
  destroy any experimental resources at end of session;
- prohibitions: touch verification CI workflows after M0 (file in CODEOWNERS),
  store a secret in the repo, disable a test, exceed the dedicated cloud project.
- anti-rationalization: plausible excuses for skipping a step are listed with
  their refutation, and the agent must not authorize itself any of them:

| Rationalization | Refutation |
|---|---|
| "Too simple for a test" | Bugs live mostly in code "too simple to be tested" |
| "I'll test at the end of the milestone" | Unverified = undone; the end of the milestone is verify-mN, not your declaration |
| "CI is slow, I'll verify locally" | The contract is CI (AUTO-4); local is a draft |
| "It's surely an environment problem" | Max 3 attempts, then BLOCKERS.md — no 4th |
| "I'll take the opportunity to clean this file" | NOTICED BUT NOT TOUCHING: + strict scope (R-08) |
| "Docs say X but code does Y, I'll follow the code" | CONTRADICTION: mandatory; do not decide alone |

### 3.2 MISSION.md — milestones with executable verification

| Milestone | Deliverable | `make verify-mN` verifies (executable, binary) |
|---|---|---|
| **M0** | CI + skeleton + empty but functional eval-harness | lint+tests green on CI; `make eval-smoke` runs (0 cases); secrets loaded; hello-world deployed on app VM |
| **M1** | Reproducible GPU node + vLLM + gateway | script `infra/gpu-up.sh` creates node from scratch; `curl gateway /v1/models` OK; TTFT/tok-s bench executed and archived; `infra/gpu-down.sh` destroys everything; **full re-creation < 20 min** timed |
| **M2** | Ingestion + RAG | test corpus ingested; POC-E1 ≥ 0.85; resolvable citations (automatic test) |
| **M3** | Agentic harness + web tools | POC-E4 ≥ 90%; SSRF/robots.txt/budgets tests pass; POC-E6 executable |
| **M4** | Cascade + UI connected | POC-E8 ≥ 85%; serverless fallback tested (simulated GPU failure: `gpu-down` under traffic → UI still responds) |
| **M5** | Full eval suite + telemetry | `make eval` complete < 20 min; HTML report with diff; cost/latency dashboard fed |
| **M6** | Hardening + final bench + report | POC-P1..P9 measured; `make demo` runs full scenario; GO/NO-GO report generated with figures |

- AUTO-4 (MUST): each `verify-mN` is a repo script, written (or validated) **before**
  the agent starts the milestone. This is the contract. If it is not executable, the milestone
  is not ready to be delegated.
- AUTO-5 (MUST): the order M0→M1 is non-negotiable: CI and reproducible infra first.
  An agent coding the harness before having the verification loop produces
  unverifiable code — failure mode #1.

## 4. Execution: sessions and human checkpoints

- Work by **milestone sessions** (not "do all M0–M6 in one go"): very long autonomous
  sessions drift; resetting context at each milestone, with re-reading of MISSION.md + JOURNAL.md,
  maintains quality.
- **4 human checkpoints of ~20 minutes**, no more:
  1. after M1: are billing and infra healthy? (read journal + cloud console)
  2. after M3: manually test 10 web/RAG requests — human intuition detects what evals still miss;
  3. after M5: read eval report, calibrate the judge (POC-R2, 30 multilingual cases graded
     manually — non-delegable);
  4. after M6: GO/NO-GO decision.
- Between checkpoints: zero supervision required. The exception channel is
  `BLOCKERS.md`: the agent logs what blocks it there and continues with something else;
  you read it when you want.

## 5. What remains human (delegating this would cause the PoC to fail)

1. **Create accounts and keys** (cloud, serverless) — by nature.
2. **The initial golden set**: the 140 multilingual eval cases (FR/DE/ES/IT/EN), especially E2/E6/E7/E9. An agent can
   generate drafts, but if the test set is written by the same model family as the one
   being tested, the measurement is worthless. Count 1–2 days of human work — this is the
   investment with the best ROI of the entire PoC.
3. **Judge calibration** (POC-R2).
4. **The 10 manual requests at checkpoint 2** and the GO/NO-GO decision.

## 6. Known failure modes of autonomous agents — and their countermeasures here

| Failure mode | Countermeasure in this setup |
|---|---|
| Declaring "it works" without proof | Only CI counts (AUTO-4); milestone report cites the CI run |
| Weakening a test to make it pass | Workflows and `verify-*` in CODEOWNERS, modifiable only by you |
| Scope drift ("I also added…") | §2 of doc 13: extensions refused by default; diff review at checkpoint |
| Forgotten cloud resources → bill | Provider-side shutdown cron (AUTO-3) + independent budget alert (AUTO-2) + `infra/inventory.sh` inventory executed at end of session |
| Secrets leaking into code/logs | Secret scanner in CI (blocking) from M0 |
| Looping on an environment bug | Rule of 3 attempts in AGENTS.md: after 3 failures on same issue → BLOCKERS.md and move on |
| Context polluted over long duration | Sessions per milestone, restart with clean context (§4) |

## 7. Operational summary — your launch checklist (half a day)

1. Create dedicated cloud project + restricted IAM key + budget alert + GPU shutdown
   scheduler. Create capped serverless key.
2. Create repo with `docs/`, `AGENTS.md`, `MISSION.md`, `Makefile`, CI, and
   `verify-m0..m6` scripts (even simple ones).
3. Write/validate the initial golden set (the real work).
4. Launch agent on M0. Return to checkpoint 1.

From there, your role is what the corpus assigns to humans from the start:
define criteria, validate at milestones, decide on ADRs. The rest runs itself —
precisely because nothing that runs itself is unverifiable.
