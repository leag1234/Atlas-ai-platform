# 11 — Engineering Standards for Implementer Agents

> **Must be read in full by every agent before writing their first line of code.**
> This document exists because the dominant failure mode of coding agents is not
> incompetence: it is **the rapid production of plausible, unspecified, untested, and
> unintegrated code**. The rules below are designed to make this failure mode impossible,
> not improbable.

## 1. Absolute Rules (violation = PR automatically rejected)

| # | Rule |
|---|---|
| R-01 | **No code without requirement.** Every PR references ≥ 1 `REQ-*` identifier. No REQ → write the spec first, or open an ADR. |
| R-02 | **Contract before code.** Schemas (OpenAPI, JSON Schema, SQL, protobuf) are merged and reviewed separately, before implementation. |
| R-03 | **Test before or with code, never after.** A PR without tests is an incomplete PR. The test must **fail** without the fix (prove this in the description). |
| R-04 | **Forbidden to disable a test or a linter** to make CI pass. `@skip`, `# type: ignore`, `eslint-disable` require a link to a ticket and an expiration date. |
| R-05 | **No model name, no prompt, no hardcoded secret** outside `model-gateway` / `prompts/` / vault. Verified by a grep in CI (AC-ARC-4). |
| R-06 | **No new dependency without justification** in the PR: why, alternatives, license, size, maintenance. Adding a library is an architectural decision. |
| R-07 | **No dead code, no "just in case" code.** What is not used is not merged. |
| R-08 | **Strict scope.** One PR = one objective. No opportunistic refactoring mixed with a feature. |
| R-09 | **Never invent an API.** If the signature of a library or an endpoint is uncertain → read it, or execute it. An unverified hypothesis is a shipped bug. |
| R-10 | **Report blockers.** If a requirement is ambiguous, contradictory, or impossible: **stop and say so**. Do not guess, do not produce a silent approximation. A reported blocker costs one hour; an implemented false hypothesis costs a week. |
| R-11 | **No simulation.** No `mock`, `stub`, or fake data must reach an executable branch in dev/staging/prod. Mocks live in tests, only. |
| R-12 | **Forbidden to "make the test pass"** by modifying the assertion rather than the code. This is a known failure mode of agents: it is treated as a serious offense. |

## 2. Definition of Ready (a task is not given to an agent without this)

A task is ready if and only if it contains:
- [ ] The covered `REQ-*`, and the reference doc.
- [ ] The input/output contracts (schemas), or the explicit instruction to write them first.
- [ ] The **verifiable acceptance criteria** (`AC-*`), formulated as tests.
- [ ] The files/modules concerned, and those **forbidden from modification**.
- [ ] Known edge cases and expected failure modes.
- [ ] The budget (latency, cost, complexity) if applicable.
- [ ] What is **out of scope** for the task.

> A task without verifiable acceptance criteria must not be accepted by the agent.
> The agent **must** return it requesting the ACs. This is a responsibility, not an option.

## 3. Definition of Done

- [ ] All `AC-*` are covered by an automated test and pass.
- [ ] Unit tests + integration; coverage ≥ 80% on modified lines (global
      coverage is not a goal, **diff** coverage is).
- [ ] Edge cases tested: empty input, huge input, unicode, timeout, provider error,
      invalid tool arguments, context exceeded.
- [ ] Evals (`09`) passed; gate green.
- [ ] Observability added: spans, metrics, structured logs for any new path.
- [ ] Cost impact declared (REQ-FIN-002).
- [ ] Security impact evaluated; new adversarial tests if the path touches an
      untrusted input.
- [ ] Documentation: the concerned reference doc is **updated in the same PR**
      (doc and code diverge the day they are separated).
- [ ] Reversible and tested migrations.
- [ ] No latency regression > 10% (measured, not assumed).

## 4. Repository Structure (monorepo)

```
/contracts        # OpenAPI, JSON Schema, protobuf — source of truth
/prompts          # versioned artifacts (semver), tested
/policies         # charter, guard policies, thresholds
/services
  /edge-bff       /orchestrator    /model-gateway
  /guardrails     /retrieval       /tool-runtime
/packages         # shared libraries (typed, no hidden I/O)
/evals            # golden sets, harness, frozen set (restricted access)
/infra            # Terraform, Helm, ArgoCD
/runbooks
/docs             # this corpus
/adr              # architectural decisions, numbered, immutable
```

- REQ-ENG-001 (MUST): one service = one `OWNERS`, one SLO, one runbook, one dashboard.
- REQ-ENG-002 (MUST): **acyclic** dependencies between services; all communication
  goes through a published contract. No access to another service's database. Never.

## 5. Code Quality

- REQ-ENG-003 (MUST): strict typing mandatory (Python: `mypy --strict` or pyright
  strict, `pydantic` at boundaries; TS: `strict: true`, no `any`). An agent system
  manipulates deep and polymorphic structures: without types, it becomes
  undebuggable within a few weeks.
- REQ-ENG-004 (MUST): boundaries validate their inputs at runtime (do not trust
  static typing against an LLM or provider response).
- REQ-ENG-005 (MUST): typed and explicit errors; forbidden `except Exception:
  pass` and error swallowing. A silent error in an agent loop produces a
  hallucination, not a crash — this is much worse.
- REQ-ENG-006 (MUST): pure functions for decision logic (routing, budget,
  compaction) → testable without network or GPU. I/O is injected.
- REQ-ENG-007 (MUST): testable determinism — any non-determinism (LLM, clock,
  random, uuid) goes through an injectable interface and is frozen in tests.
- REQ-ENG-008 (MUST): conventional commits, trunk-based, branches < 3 days, PR < 400
  lines of diff. A 2,000-line PR generated by an agent **is not reviewable** and
  will be rejected without reading.

## 6. Tests — Hierarchy

| Type | What it covers | Network? | Speed |
|---|---|---|---|
| Unit | pure logic, budgets, parsing, compaction | no | < 1 s |
| Contract | schema compliance, backward compatibility | no | < 5 s |
| Integration | service + db + dependencies (testcontainers) | local | < 60 s |
| LLM (recorded) | replay of **recorded** model responses (cassettes) → deterministic | no | fast |
| LLM (live) | real calls, nightly, capped budget | yes | slow |
| Adversarial | `07` §8 | depends | — |
| Load | `10` | yes | nightly |

- REQ-ENG-009 (MUST): CI tests per PR **do not depend** on a live LLM call
  (cost, flakiness). Recorded cassettes are used. Live evals run in nightly
  and in release gate.
- REQ-ENG-010 (MUST): zero flaky tests tolerated. An unstable test is fixed or removed
  within 48h — never "re-run".

## 7. CI/CD — Pipeline (blocking in this order)

```
lint + format → typecheck → build → unit tests → contract tests
→ integration tests → grep R-05 → SBOM + vulnerability scan
→ secret scan → smoke evals (L0/L1) → security evals (L4)
→ [merge] → staging → full evals (L2/L3/L5) → canary → prod
```

- REQ-ENG-011 (MUST): CI is **the** definition of quality. What is not verified
  by CI is not a rule, it is a wish. Any rule in this corpus that can be automated
  **must** be.

## 8. ADR

- REQ-ENG-012 (MUST): any structural decision → an ADR (`/adr/NNNN-titre.md`):
  context, considered options, decision, consequences, status. ADRs are **immutable**:
  they are replaced (`superseded by ADR-NNNN`), not rewritten.
- REQ-ENG-013 (MUST): an agent wishing to deviate from an ADR **must** open a
  competing ADR and wait for human decision. It does not bypass.

## 9. Agent Work Protocol

1. **Read**: `README.md` → the task's reference doc → the concerned contracts →
   the module's existing code. Do not start writing before this.
2. **Plan**: produce a written plan (touched files, contracts, planned tests,
   risks, open questions). The plan is reviewed **before** implementation for any
   non-trivial task.
3. **Ask blocking questions now**, not halfway through (R-10).
4. **Implement** in small verifiable increments; run tests at each step.
5. **Self-verify** against the Definition of Done, point by point, explicitly.
6. **Report honestly**: what works, what doesn't, what wasn't tested, assumptions made,
   debt introduced. **An optimistic and false report is the gravest possible offense
   in this project**: it destroys the team's ability to trust the entire work of the
   agents, including the good.

### Specific Behavioral Prohibitions
- Do not claim to have executed a test that was not.
- Do not "fix" a failing test by weakening its assertion (R-12).
- Do not expand a task's scope without mandate (R-08).
- Do not delete code you do not understand; ask.
- Do not generate comments that paraphrase the code; comment on the **why**.
- Do not produce files `*_v2`, `*_new`, `*_final`. Modify, Git versions.

### Observability Markers (adapted from Antigravity, Apache-2.0)
Behavioral rules are not unit-tested; they are audited. Every agent **must** emit
these greppable prefixes in their logs and reports when the situation arises:
`CONTRADICTION:` (spec/code/docs in conflict), `RISK:` (dangerous request or state),
`NOTICED BUT NOT TOUCHING:` (defect out of scope), `ASSUMPTION:` (assumption made,
never silent — cf. R-10), `Source:` (introduced API/command, with provenance — cf. R-09).
A grep of these markers on transcripts is part of the review at checkpoints; their
**total absence** on a non-trivial task is itself a signal (no assumption, no conflict
encountered? hardly credible).

## 10. Human Review — What Remains Non-Delegable

- Code of conduct and security policies (`05`, `07`).
- Architectural decisions (ADR) and purchasing decisions (GPU, providers).
- Content of business eval sets and the frozen set.
- Any PR touching: tenant isolation, guardrails, secret management, data
  migrations, budgets.
- Post-mortems.

> The rest can be largely automated. It is precisely because these five points
> remain human that the rest can be safely automated.
