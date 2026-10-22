# 12 — Roadmap, milestones and organization

## 1. Sequencing — logic

The order is non-negotiable, and it is counter-intuitive: **we build the evaluations and the gateway before the first visible feature.** A generative system without measurement does not improve; it drifts. Any inversion of this order has a known cost: rework at the first model change.

## 2. Phase 0 — Foundations (weeks 1–4)

| Deliverable | Doc | Exit Criteria |
|---|---|---|
| Validated spec corpus, ADR 001–008 finalized | all | Signed by architecture + security + business |
| OpenAPI contracts + merged schemas | `08` | Clients generated |
| `model-gateway` with 2 qualified providers | `03` | AC-ARC-1: switch < 1 h proven |
| `eval-harness` + first 50 golden cases | `09` | Runs in CI, < 3 min |
| Charter v0.1 | `05` | Business + legal review |
| CI/CD skeleton, IaC, observability | `10`, `11` | Full pipeline green |
| Legal validation of licenses for selected models | `03` | Written opinion |
| Real measurement of hypotheses A-1..A-6 | `01` | Recalculated FinOps model |

**Exit Gate**: We can swap one model for another via a config PR, and objectively measure the impact. Nothing else is required.

## 3. Phase 1 — Internal Pilot (weeks 5–12)

Scope: chat + RAG on 2–3 corpora + 3–5 tools, 1 pilot department (~50 users).

| Deliverable | Doc |
|---|---|
| Orchestrator (state machine, budgets, recovery) | `06` |
| Hybrid RAG + reranker + verified citations | `06` |
| IN/OUT Guardrails (small self-hosted models) | `07` |
| UI with clickable citations, feedback, step transparency | `08` |
| Multi-tenant + RLS + leak tests | `08` |
| Quality / cost / SLO dashboards | `04`, `10` |
| Business golden set ≥ 300 cases, adversarial suite ≥ 200 cases | `09` |
| Red team #1, DPIA, AI Act classification | `07` |

**Exit Gate (Internal GA)**:
- G1–G7 of `09` §6 green.
- Zero cross-tenant leaks, zero side effects from indirect injection.
- €/request ≤ budget; TTFT p95 < 1.2 s.
- Pilot satisfaction ≥ 4.0/5; ≥ 60% weekly usage.
- 4 runbooks exercised in a game day.

## 4. Phase 2 — Enterprise Rollout (months 4–8)

| Deliverable | Doc |
|---|---|
| S→M→L routing cascade + evaluated classifier | `03` |
| Self-hosting of **small** models (embeddings, rerank, guards, routing) — the best infra ROI | `04` §4 |
| Optimized prefix caching, semantic cache | `04` |
| Team assistants, internal MCP connectors | `06` |
| Code execution sandbox | `06` |
| SFT/DPO on LoRA adapters (format, tone, tool-calling, anti-sycophancy) | `05` |
| Long-term memory, projects/spaces | `06` |
| Red team #2 (external) | `07` |

**Exit Gate**: −40% cost/request at constant quality (AC-INF-4); business eval suite ≥ 90% of the reference proprietary model (CS-3); ≥ 60% DAU on target.

## 5. Phase 3 — Optimization and potential self-hosting (months 9–15)

Triggered **only** if the calculation of `04` §4 is crossed with real volumes.

| Deliverable | Condition |
|---|---|
| Distillation of an L model to an S on our tasks | ROI ≥ 20% demonstrated |
| Self-hosting of the main model (vLLM/SGLang, FP8, spec decoding) | U > 60% sustained **and** 1–2 FTE SRE/ML allocated |
| Batch pool on spot instances | Significant batch volume |
| Multimodal (image input) | Validated business demand |
| External multi-tenant extension / product | Business decision |

> Reminder: Crossing this threshold without dedicated FTEs is the most common failure scenario for this type of project. The GPU is not the cost; operations are.

## 6. Minimum Team

| Role | FTE | Role of AI Agents |
|---|---|---|
| Tech lead / architect | 1 | Writes specs and ACs, reviews every ADR |
| ML Engineer (post-training, evals) | 1 | Leads evals and fine-tunes |
| Backend/Platform Engineer | 1–2 | Supervises agents on services and contracts |
| SRE / infra | 0.5 → 1 | IaC, SLO, runbooks |
| Security / compliance | 0.5 | Guards, DPIA, AI Act, red team |
| Product / business | 0.5 | Golden sets, charter, adoption |
| **Implementation Agents** | — | Code under spec (`11`), tests, migrations, tooling, docs |

Agents produce the volume; humans produce **the specifications, the acceptance criteria, and the decisions**. Any attempt to reverse this ratio ("agents decide, humans review") fails: it is the return of vibe coding, at a larger scale and faster.

## 6bis. Adoption and change management

Criterion CS-1 (≥ 60% active at 30 days) is not achieved by technical quality alone. Plan from Phase 1:
- **Champions**: 2–3 liaisons per pilot department, trained before launch, who report use cases and feed the golden set (`09` — it is the same work).
- **Training**: Short sessions oriented toward real department use cases, not "general AI demo"; maintained internal prompting guide.
- **Visible loop**: 👍/👎 feedback results in a monthly user changelog ("you reported X, it is fixed") — this is the cheapest adoption lever in existence.
- **Honest measurement**: Also track *shadow IT* (persistent use of public AI despite the internal tool): this is the most reliable failure indicator, before surveys.
- REQ-ADO-001 (MUST): A product owner is responsible for these actions; they appear in the same plan as technical deliverables in phase gates.

## 7. Major Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Underestimating post-training / behavior and delivering an assistant that is "technically correct, humanly mediocre" | Zero adoption | `05` from phase 0 (charter + behavior evals) |
| Premature GPU purchase | Wasted Capex, immobilized FTEs | ADR-001, threshold `04` §4, quarterly review |
| Vendor lock-in despite everything | Loss of "open" benefit | `model-gateway` + monthly switch game day |
| Guards too strict → user workarounds (shadow IT to public AI) | Leak risk **worse** than initial risk | Measure `refusal_precision` and `guard_false_positive` (`09` §5) |
| Reference open-weight model changes every 2 months | Instability | The eval pipeline makes model changes routine — that is precisely the goal |
| Indirect injection via corporate documents | Sev 1 | REQ-SEC-015: **architectural** defense, not just via prompt |
| Fine-tuning degrades alignment | Sev 1 | REQ-PT-011: Security suite replayed after every fine-tune, blocking |
| Cost explosion due to long context | Budget | Hard budgets, context ceiling, disciplined RAG |

## 8. Rituals

- **Weekly**: Quality review (L6 metrics), incident review, false refusal triage.
- **Monthly**: `model-review` (has the portfolio of `03` changed?), FinOps review (has the threshold of `04` §4 been crossed?), game day.
- **Quarterly**: Self-hosting decision, restore test, license review, opening of the frozen set before major release.
