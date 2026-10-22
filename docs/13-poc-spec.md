# 13 — ATLAS-0 PoC Specification

> Target duration: 6 weeks. Target infra budget: < €800 all-inclusive.
> Objective: demonstrate on real data that a "Claude-like" assistant built on
> open-weight models reaches a measured quality level sufficient to launch
> phase 1 — or demonstrate the opposite, which is also a success for the PoC.
> This document is **self-contained**: an implementation agent must be able to execute it
> by reading it alongside `14-implementation-autonome.md`, without reading the entire corpus (the REQs from the corpus cited here are included with their substance).

## 1. What the PoC DOES

| Id | Capability | Detail |
|---|---|---|
| POC-F1 | Multi-turn streaming chat | SSE, generation stop, persisted history |
| POC-F2 | RAG with citations | Ingestion of pdf/docx/md/html, hybrid BM25+dense+reranker, clickable citations resolved to source passage |
| POC-F3 | **Automatic web search** | The model decides to search; SerpApi API (Google Search + Google News), queries reformulated by the model, `gl`/`hl` localization adapted to the question language |
| POC-F4 | **Web page reading (scraping)** | Retrieval + extraction of main content (trafilatura), respect for robots.txt, local cache, content used in reasoning **with URL + date citation** |
| POC-F5 | Tool calls in agent loop | State machine with hard budgets; tools: `web_search`, `web_fetch`, `rag_search`, `calculator` |
| POC-F6 | 2-level cascade | Local model (default) + escalation to a serverless EU L model for complex requests, via the gateway |
| POC-F7 | Continuous automatic evaluation | Eval suite executable by CI and on-demand, HTML report, comparison between runs |
| POC-F8 | Minimal telemetry | tokens, cost, TTFT, tok/s, prefix cache hit rate, per request; CSV export/simple dashboard |

## 2. What the PoC does NOT do (assumed, do not implement)

Hardened multi-tenancy (single organization, basic auth via shared password or
simple OIDC) · complete guardrails (only a minimal input filter) · SSO/SCIM ·
high availability · code execution sandbox · fine-tuning · long-term memory ·
mobile · formal compliance (but **no real sensitive data** in the PoC:
public or internal non-confidential document corpus only — this is the trade-off
that allows speed).

Rule: any request for extension during the PoC is refused by default and noted for
phase 1. The scope is frozen upon signature of this document.

## 3. PoC Architecture (2 machines)

```
[ CPU VM "app" — small, ~€10-20/month ]
  ├─ UI (Open WebUI or LibreChat)          ← we do NOT develop a UI
  ├─ gateway (LiteLLM) ── fallback ──────────► EU serverless provider (L model)
  ├─ harness (FastAPI, ~800 lines): state machine, tools, budgets
  ├─ SerpApi client (search) + fetcher (trafilatura) + cache
  ├─ Postgres + pgvector (conversations, chunks, telemetry, eval results)
  └─ eval-harness (CLI)

[ GPU Node — ephemeral, reconstructible by script ]
  └─ vLLM: main model + (embeddings + reranker served via same vLLM or TEI)
```

Imposed decisions:
- POC-A1: the GPU node is **disposable** — provisioned by script (Terraform or provider
  CLI + cloud-init), weights on persistent Block Storage volume reattachable.
  No manual installation via SSH.
- POC-A2: the app VM is **permanent** (it holds the state); the GPU node is turned off
  at night and on weekends by cron (`scheduler on/off`) → ÷2 to ÷3 on the bill.
- POC-A3: the entire application only knows the gateway (one URL). Changing model or
  GPU tier = config only.
- POC-A4: vLLM prefix caching enabled; prompt block order stable (system →
  tools → docs → history) — verified by a test.

## 4. PoC Models

| Role | Initial Choice | Tested Replacement |
|---|---|---|
| Main (local) | Qwen3-30B-A3B, FP8 (Apache-2.0) | higher tier if evals plateau |
| Escalation (serverless EU) | an open-weight L model hosted in EU (GLM/DeepSeek/Kimi class) | second provider as fallback |
| Embeddings | open-weight multilingual model, dim ≤ 1024 | — |
| Reranker | open-weight cross-encoder | — |
| Eval Judge | the serverless L model (≠ evaluated model) | — |

## 5. Web tools — precise specification (POC-F3/F4)

- POC-W1: `web_search(query, n=5, lang)` queries **SerpApi** (Google Search; Google
  News for current affairs questions). Return: title, URL, snippet, date if available.
  `gl`/`hl` parameters aligned with detected question language (a question in
  German searches on google.de in German). **Starter Plan ($25/month, 1,000
  searches)**; application quota in harness: max 3 searches/user request,
  monthly counter with clean stop at 90% of quota, cache for identical searches
  (TTL 1 h) to avoid burning quota on repeated evals — the SerpApi FAQ states
  that only successful searches are counted, and eval runs are the biggest
  consumers.
  *Residency note*: SerpApi is a US provider; acceptable for the PoC since only
  **search queries** (never documents nor full conversations) are transmitted to it
  and the PoC corpus is non-sensitive. For production, this point goes back through
  review REQ-INF-002/REQ-CMP-004 (DPA, transfer clauses) or via an EU alternative —
  decision to be investigated in phase 1, not in the PoC.
- POC-W2: `web_fetch(url)`: GET with identified User-Agent, 15 s timeout, max size
  2 MB, **respect for robots.txt**, main content extraction by trafilatura,
  truncation to 8,000 tokens with handle for continuation, disk cache TTL 24 h.
- POC-W3: minimal but non-negotiable security, even in PoC:
  - denylist of private networks (SSRF: 10.x, 172.16–31.x, 192.168.x, 169.254.x,
    localhost, cloud metadata 169.254.169.254);
  - web content = **untrusted**: framed by delimiters + instruction to never
    execute instructions contained within; no side-effect tools exist in the
    PoC (all tools are read-only), which neutralizes most of the risk;
  - max 8 fetches per user request.
- POC-W4: any assertion derived from the web includes URL + consultation date in the
  response. The citation verification pipeline (POC-E5) also applies to the web.
- POC-W5: typical search loop: reformulate → search → select 2–3 URLs →
  fetch → synthesize → if insufficient, iterate (max 3 iterations, budget POC-P6).

## 6. Expected Performance (measurable targets)

Loaded as thresholds in the eval-harness; a missed figure = explicit decision
(accept/correct/upgrade tier), not a shrug.

| Id | Metric | PoC Target | Measurement |
|---|---|---|---|
| POC-P1 | TTFT p95 (chat without tool) | < 2.0 s | scripted load bench |
| POC-P2 | Decoding throughput p95 | > 30 tok/s | idem |
| POC-P3 | End-to-end latency p95, RAG request | < 12 s | idem |
| POC-P4 | End-to-end latency p95, web request (2 fetches) | < 30 s | idem |
| POC-P5 | Sustained concurrency without > 20% degradation | 8 parallel requests | idem |
| POC-P6 | Hard budgets per request | ≤ 10 tool calls, ≤ 120 s, ≤ €0.05 | integration test that triggers them |
| POC-P7 | Prefix cache hit rate | > 50% | vLLM metric |
| POC-P8 | Average cost / request (amortized GPU + serverless) | < €0.02 | telemetry |
| POC-P9 | Business hours availability over last 2 weeks | > 97% | uptime monitor |

## 7. Automatic Quality Validation (the heart of the PoC)

Versioned golden set in Git: **minimum 140 cases**, distributed across **FR, DE, ES, IT, EN
(no language < 15% of the set)**, scores calculated and reported **per language**:

| Suite | Cases | Verification | GO Threshold |
|---|---|---|---|
| POC-E1 retrieval | 40 questions → expected doc/chunk (corpus and questions multilingual, including question in a language ≠ document language) | **deterministic**: recall@8, MRR | recall@8 ≥ 0.70 (PoC gate; target 0.85) |
| POC-E2 RAG end-to-end | 30 Q/A on corpus | LLM judge (accuracy/completeness rubric) + presence of citation | ≥ 4.0/5 average |
| POC-E3 honest refusal | 10 questions with no answer in corpus | deterministic (regex "cannot find") + judge | 10/10: zero hallucination |
| POC-E4 tool-calling | 20 scenarios (correct tool, correct args, recovery on injected error) | deterministic (assertions on trace) | ≥ 90% |
| POC-E5 citation fidelity | sample of E2 + web responses | NLI/judge verifier: each citation supports the sentence | ≥ 0.90 |
| POC-E6 **web Q/A** | ≥5 stable web facts verified, multilingual (LIGHTENED PoC set; SerpApi quota spared; extension 20 cases + post-PoC current affairs) | judge + source citation | executable, sourced facts |
| POC-E7 behavior | 15 cases anti-sycophancy / honesty / format (mini-charter) | calibrated judge | ≥ 4.0/5 |
| POC-E8 routing | 30 requests labeled simple/complex | deterministic: confusion matrix | ≥ 85%; zero silent "complex→local" on critical cases |
| POC-E9 **translation** | available business cases (false friends, terminology; FLORES post-PoC) | bilingual judge | ≥ 4.0/5; no inverted meaning |

Cross-cutting threshold (POC-EL, PoC relaxed): the constraint on language distribution (no language < 15%) is INDICATIVE for the PoC (Italian at ~11% does not block). Ultimately: for each judged suite (E2, E6, E7, E9), **the gap between
the best and worst language ≤ 15%** — this is the equal treatment test.
A model that passes averages but fails this threshold is a NO-GO just the same.

Execution rules:
- POC-R1: `make eval` executes everything, produces a timestamped HTML report with diff vs the
  previous run **and breakdown by language**, stores results in database.
  Duration < 20 min, cost < €3.
- POC-R2 (PoC): the production judge (glm-5.2) is calibrated by CROSS-AGREEMENT with a
  reference judge from another family (gpt-oss-120b): Cohen's κ calculated on an
  eval sample. HUMAN calibration (30 scores, κ≥0.7) remains a pre-GA action.
- POC-R3: E6 cases (web) include the creation date of the correction key; an
  expired case (reality has changed) is marked `stale`, not counted as failure.
- POC-R4: CI: `make eval-smoke` (15 representative cases covering ≥ 3 languages, < 3 min,
  < €0.3) on every PR; full suite nightly + on every model/prompt change.

## 8. End of PoC GO / NO-GO Criteria

**GO phase 1** if: all thresholds §6 and §7 reached with the local model (escalations
≤ 25% of requests) **or** reached with a higher GPU tier whose projected cost
respects REQ-NFR-005 (< €0.015/req at scale). Otherwise: quantified gap report and
explicit decision (change model, revise targets, or stop).

## 9. Leased Infrastructure — quantified recommendation (July 2026, re-verify at quote)

| Option | Machine | Observed Price | Recommended Role |
|---|---|---|---|
| **Recommended: Scaleway L40S-1-48G** (Paris) | 48 GB VRAM, ~8 vCPU, NVMe scratch | **~€1.47/h ex-VAT**; ~€250–350/month in business hours with scheduled shutdown (POC-A2) | Main GPU node: Qwen3-30B-A3B FP8 + embeddings + reranker. Native FP8 (Ada). Hourly billing = perfect for nightly shutdown. EU/France, consistent with sovereignty constraint. |
| Scaleway L4-1-24G | 24 GB | ~€0.75–0.90/h | Ultra-frugal variant (30B-A3B in tight Q4); keep as backup |
| Scaleway H100-1-80G | 80 GB | ~€2.7–3.0/h | The "tier above" to test a ~120B MoE at end of PoC (a few days suffice) |
| Hetzner GEX130 (RTX 6000 Ada 48 GB) | monthly dedicated | ~€900/month flat (re-verify) | Only if PoC had to run 24/7 — not our case, Scaleway hourly wins |
| RunPod/Vast (spot L40S) | 48 GB | ~€0.26–0.50/h | Cheapest, but non-EU/preemptible: acceptable for disposable benches, not for reference PoC |
| App VM (Scaleway DEV/PRO or Hetzner CX) | 4–8 vCPU, 16 GB RAM | ~€10–25/month | UI, gateway, harness, SerpApi client, Postgres |

PoC Budget 6 weeks, realistic: GPU ~€350–500 (business hours + few nightly
evals + 3–4 days of H100 at end of PoC) + VM ~€30 + serverless L (escalations + judge)
~€50–120 + SerpApi Starter 2 months ~€45 + storage ~€10 ≈ **€500–700**.

- POC-I1: budget alert at provider at 50% and 80% of €800; application circuit-breaker
  on serverless (monthly cap in gateway).
- POC-I2: scheduled GPU shutdown is in place **from day one** (this is budget
  lever #1); morning restart reloads the model automatically
  (< 10 min, weights on persistent volume).


> **Clarification M5 (PoC)**: the judge calibration in M5 is a CROSS-MODEL calibration (production judge glm-5.2 vs reference judge gpt-oss-120b, family distinct from judge glm-5.2 AND tested system Qwen), not a human calibration. Human calibration (30 scores, κ≥0.7) remains a **pre-GA** action, non-blocking for the PoC. See verify-m5.
