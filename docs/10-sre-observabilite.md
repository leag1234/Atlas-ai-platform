# 10 — SRE, Observability, Operations

## 1. SLO and Error Budget

| SLI | SLO (Internal) | SLO (Product) | Window |
|---|---|---|---|
| Availability (`2xx+4xx` / total) | 99.5% | 99.9% | Rolling 30 days |
| TTFT p95 | < 1.2 s | < 1.0 s | 30 days |
| TPOT p95 (inter-token) | > 25 tok/s | > 30 tok/s | 30 days |
| Agentic Task Failure Rate | < 5% | < 3% | 30 days |
| RAG Index Freshness | < 15 min after source modification | same | continuous |

- REQ-SRE-001 (MUST): Explicit error budget. If consumed, **development of new features stops** in favor of reliability. Written rule, applied.
- REQ-SRE-002 (MUST): Alerts based on **symptoms** (SLO burned) and not causes (high CPU). No non-actionable alerts — every alert has a runbook.

## 2. Telemetry

- REQ-OBS-001 (MUST): End-to-end **OpenTelemetry**. A unique `trace_id` traverses BFF → orchestrator → gateway → provider → tools. Each agentic step is a span with: model, tokens (input / output / cached), cost, latency, routing decision, guard results.
- REQ-OBS-002 (MUST): Minimum exposed metrics: `llm_requests_total{tenant,task_class,model,provider,status}`, `llm_tokens_total{direction,cached}`, `llm_cost_eur_total{…}`, `llm_ttft_seconds`, `llm_tpot`, `prefix_cache_hit_ratio`, `tool_calls_total{tool,status}`, `guard_blocks_total{stage,category}`, `retrieval_latency_seconds`, `escalation_total`.
- REQ-OBS-003 (MUST): **Prompt logs** — treated as personal data: encrypted, restricted and logged access, limited retention (30 days by default), PII redaction. Access to prompt logs in production **must** be justified and traced. This is the most sensitive file in the system.
- REQ-OBS-004 (MUST): Sampling: 100% of error traces, 100% of guard blocks, 1–5% of nominal traffic (the rest in aggregated metrics).
- REQ-OBS-005 (MUST): Standard dashboards: Quality (L6 of `09`), Cost (`04`), Reliability (SLO), Security (guards, detected injections).

## 3. Deployment

- REQ-SRE-003 (MUST): GitOps. No manual `kubectl apply` in prod. Desired state is in Git; drift is detected and corrected.
- REQ-SRE-004 (MUST): Progressive deployments (canary 5% → 25% → 100%) with automatic analysis and automatic rollback (cf. REQ-EVA-011).
- REQ-SRE-005 (MUST): **Feature flags** for: model per task class, provider, tool activation, guard thresholds, RAG activation. They constitute the kill switch (REQ-SEC-020) and allow reacting without deployment.
- REQ-SRE-006 (MUST): Reversible database migrations, tested on a copy of prod.

## 4. Resilience

- REQ-SRE-007 (MUST): *Circuit breaker* per provider; on open → switch to fallback, alert, no user failure.
- REQ-SRE-008 (MUST): **Graceful** cascading degradation, in this order:
  1. L model unavailable → M;
  2. RAG unavailable → respond without documents **stating this explicitly**;
  3. Tools unavailable → respond without tools stating this;
  4. Everything unavailable → honest error message, not a made-up response.
  **Never** degrade silently: a response without RAG presented as based on documents is worse than an error.
- REQ-SRE-009 (MUST): Queue + backpressure; under overload, we queue and inform, we do not let requests time out indiscriminately.
- REQ-SRE-010 (MUST): Encrypted backups, restoration **tested** quarterly (RTO 4 h / RPO 15 min, REQ-NFR-008). A backup that has not been restored does not exist.

## 5. Runbooks (to be written, one file per scenario)

| Id | Scenario | Trigger |
|---|---|---|
| RB-01 | Inference provider degraded / unavailable | Circuit breaker open |
| RB-02 | Cost explosion (routing drift, tool loop, giant prompt) | Budget at 80% before term |
| RB-03 | Drop in prefix cache hit rate | `prefix_cache_hit_ratio` < 40% |
| RB-04 | Quality regression detected in canary | Auto rollback → analysis |
| RB-05 | Suspicion of cross-tenant leak | **Sev 1**: Tenant cutoff, log freeze, incident response team |
| RB-06 | Successful prompt injection with side effect | **Sev 1**: Tool kill switch, revocation, audit |
| RB-07 | Corrupted or stale RAG index | Re-indexing, graceful degradation in the meantime |
| RB-08 | Model withdrawn by its publisher / license change | Provider or model switch, legal review |
| RB-09 | Wave of false refusals | Threshold adjustment via flag, added eval cases |

- REQ-SRE-011 (MUST): Each runbook contains: detection, impact, immediate mitigation (< 5 min), correction, communication, and the reference of the eval to be added afterwards.
- REQ-SRE-012 (MUST): Monthly **game days** — provider switch, kill switch, restoration, RB-05 in simulation. A runbook never exercised is fiction.

## 6. Post-mortem

- REQ-SRE-013 (MUST): Blameless, within 5 business days, with root cause, timeline, dated and **assigned** corrective actions, and an eval case created (REQ-EVA-007).

## 7. Acceptance Criteria

- AC-SRE-1: A single trace allows reconstructing a complete request, cost included.
- AC-SRE-2: The 9 runbooks exist; ≥ 4 have been exercised in a game day.
- AC-SRE-3: Automatic rollback triggered successfully during a deliberately triggered test.
- AC-SRE-4: Restoration tested and documented within the last 3 months.
