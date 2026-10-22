# 04 — FinOps: Cost Model, Thresholds, Levers

> This document contains the most consequential decision of the project: **when (not) to
> buy GPUs**. The price figures are orders of magnitude for Q3 2026 and
> **MUST** be re-sourced via quotes before any commitment. The *method*, however, holds.

## 1. Principle

- **REQ-FIN-001 (MUST)**: Cost is a product metric, exposed on the same footing as
  latency. Dashboard: €/request, €/active user/month, €/tenant, € per
  feature.
- **REQ-FIN-002 (MUST)**: Any PR modifying the inference path must declare in its
  description the estimated impact on €/1,000 requests. The CI publishes the actual measurement from
  the load benchmark.
- **REQ-FIN-003 (MUST)**: Hard budgets per tenant and global, with a circuit breaker
  (graceful shutdown + alert) at 100% of the budget and an alert at 80%.

## 2. Unit Cost Breakdown

```
C_request = C_prefill + C_decode + C_embedding + C_rerank + C_guardrails
            + C_tools + C_storage + C_platform

C_prefill = (input_tokens × (1 - cache_hit_rate)) × input_price_per_token
C_decode  = output_tokens × output_price_per_token      # ~3 to 5× the input price
```

Two observations that drive all optimizations:
1. **Prefill dominates in RAG.** With 6,000 input tokens and 700 output tokens, the input
   represents ~60–70% of the cost despite its lower unit price. → Attack the context size and prefix cache first, not the response length.
2. **Reasoning tokens ("thinking") are billed as output.** A reasoning model can multiply the cost by 5–10. → Enable reasoning mode only
   for task classes that justify it (REQ-INF-003).

## 3. Orders of Magnitude (to be revalidated by quotes)

| Item | Q3 2026 Range |
|---|---|
| Serverless open-weight, S model (~30B MoE) | ~€0.05–0.20 / Mtok input; €0.20–0.60 / Mtok output |
| Serverless open-weight, L model (~1T MoE) | ~€0.40–1.00 / Mtok input; €1.50–3.00 / Mtok output |
| On-demand H100 80 GB GPU | ~€2.0–3.0 / h |
| 1-year reserved H100 GPU | ~€1.3–2.0 / h |
| Spot / preemptible GPU | −60 to −80% vs on-demand |
| 8×H100 Node (self-hosted L model) | ~€11,000–17,000 / month reserved |

## 4. Serverless → Self-hosting Switch Threshold (the calculation to perform)

```
Cost_selfhost_per_Mtok = (node_hourly_cost × 730) / (tok_per_s_throughput × 3600 × 730 × U) × 1e6
                       = node_hourly_cost / (tok_per_s_throughput × 3600 × U) × 1e6

  U = actual utilization rate (fraction of time the GPU is actually decoding)
```

**Worked Example — L model on an 8×H100 node:**
- Node cost: €15 / h.
- Realistic aggregated throughput in batched mode: ~4,000 output tokens / s (to be measured!).
- At `U = 100%`: 15 / (4,000 × 3,600) × 1e6 ≈ **€1.04 / Mtok output**.
- At `U = 30%` (reality of internal traffic during business hours): ≈ **€3.5 / Mtok**.
- Comparable serverless price: €1.50–3.00 / Mtok.

> **Conclusion (ADR-001).** Self-hosting a frontier model is only profitable
> beyond approximately **60–70% sustained utilization**, which, with our hypotheses
> A-1 to A-6, requires an order of magnitude of **several billion output tokens
> per month** — i.e., ~10× our volume at 2,000 DAU. **We do not buy GPUs in Phase 1
> nor in Phase 2.** We re-evaluate the calculation every quarter with actual volumes.

Special cases where self-hosting wins anyway, and which must be identified:
- **XS/S models** used at very high frequency (routing, guardrails, embeddings,
  reranking, classification): they run on 1 cheap GPU (L4/L40S), at high
  utilization rates, and represent a huge volume of calls. → **Hosting these
  models from Phase 2** is often the best ROI of the project.
- Massive **batch** processing (ingestion, re-indexing, synthetic data
  generation): spot GPUs, U ≈ 100%. → Very profitable.
- Absolute regulatory constraint forbidding any third party.

## 5. Reduction Levers, by Decreasing ROI

| # | Lever | Typical Gain | Effort | Requirement |
|---|---|---|---|---|
| 1 | **Prefix caching** (system prompt + tools + stable docs at head of prompt) | −40 to −70% on prefill | Low | REQ-FIN-004 (MUST) |
| 2 | **Routing Cascade** S→M→L | −40 to −70% global | Medium | REQ-INF-003 |
| 3 | **Disciplined Context**: Precise RAG rather than long context; prune history via summarization | −30 to −50% on input | Medium | REQ-FIN-005 (MUST) |
| 4 | **Exact cache + Semantic cache** of responses | −10 to −30% (depends on redundancy of internal questions; often high) | Low | REQ-FIN-006 (SHOULD) |
| 5 | **Self-host small models** (embeddings, rerank, guards, routing) | −60 to −90% on these items | Medium | REQ-FIN-007 (SHOULD) |
| 6 | **Selective reasoning mode** | −50% on tasks where it was unnecessary | Low | REQ-INF-003 |
| 7 | **Batch API / off-peak** for asynchronous tasks | −50% | Low | REQ-FIN-008 (SHOULD) |
| 8 | **Distillation** of an L model to an S model on our tasks | −70 to −90% on covered tasks | High | Phase 3, cf. `05` |
| 9 | FP8 Quantization (self-hosting) | −40% VRAM, +throughput | Low | REQ-INF-008 |

- **REQ-FIN-004 (MUST)**: The order of prompt blocks is **stable and normalized**
  (system → tools → policies → documents → history → current message). Any
  variation at the head of the prompt destroys the prefix cache. Prohibit injection of
  timestamps, UUIDs, or random content at the start of the prompt — this is the most
  frequent and costly error.
- **REQ-FIN-006**: The semantic cache **MUST NOT** be shared between tenants
  (data leak + out-of-context responses). Cache key = `hash(tenant_id, corpus_version, prompt_normalisé)`.

## 6. Beware of False "Free" Costs

Self-hosting shifts costs more than it eliminates them:
on-call SRE/ML engineers, capacity planning, GPU failure management, driver
updates, load testing, weight management. Count **1 to 2 FTEs** dedicated as soon as a
GPU cluster is in 24/7 production. This cost often exceeds the targeted savings at our
scale — this is the main reason for ADR-001, even more so than the calculation in §4.

## 7. Acceptance Criteria

- AC-FIN-1: Real-time € dashboard (cost per request, tenant, task class, model).
- AC-FIN-2: Prefix cache hit rate is > 60% in prod, alert if < 40%.
- AC-FIN-3: The budget circuit breaker is tested (integration test, not just in theory).
- AC-FIN-4: Monthly cost review producing a documented decision on the threshold in §4.
