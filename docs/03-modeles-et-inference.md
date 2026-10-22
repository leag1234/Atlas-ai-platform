# 03 — Models and Inference Layer

> **Freshness Warning.** The ranking of open-weight models changes every
> 4 to 8 weeks. This document establishes a **selection method** and a **portfolio as of
> 14/07/2026**. The portfolio is reviewed monthly (ritual `model-review`, cf. `12`).
> No component outside `model-gateway` may depend on a model name (REQ-ARC-006).

## 1. Vocabulary — do not confuse

- **Open source (OSI)**: weights + code + data + training pipeline published.
  Almost no frontier models meet these criteria.
- **Open weight**: downloadable weights, variable license, unpublished data.
  This is what we use. The cited models (Qwen, GLM, DeepSeek, Kimi, Llama, Gemma)
  are **open-weight**, not open source.
- **REQ-MOD-001 (MUST)**: internal and external communication must use "open-weight".
  Claiming "open source" would expose the company to well-founded criticism.

## 2. Selection Matrix (Weighted)

| Criterion | Weight | Score |
|---|---|---|
| License (Apache-2.0 / MIT = 1.0; custom license with caps = 0.5; NC = 0) | 25% | Blocking if 0 |
| Score on **our** business evals (not public benchmarks) | 30% | cf. `09` |
| Cost per request at constant quality | 20% | cf. `04` |
| Availability at ≥ 2 EU providers + self-hostable | 10% | REQ-NFR-009 |
| Quality of tool-calling and format adherence | 10% | dedicated eval |
| Support for vLLM/SGLang, FP8 quantization | 5% | |

- **REQ-MOD-002 (MUST)**: the license must be validated by legal **before** any POC.
  Verify: commercial use, user caps, geographic restrictions,
  clauses on model outputs, attribution obligations.
  Llama and certain "custom" licenses impose conditions; Apache-2.0 (Qwen) and
  MIT (GLM, DeepSeek, Phi) are the cleanest.
- **REQ-MOD-003 (MUST)**: a model enters production only after passing the
  `09` eval suite with a score ≥ the current champion, or a better quality/cost ratio.
- **REQ-MOD-005 (MUST)**: quality **in each target language (FR, DE, ES, IT,
  EN)** and tokenizer efficiency in these languages (tokens/word measured on an internal
  reference corpus per language) are part of the matrix in §2. A model excellent in
  English but weak in a target language is disqualified for the M/L conversational class. At equal quality, a tokenizer 25% more efficient on our languages =
  25% lower input cost: an economic criterion, not cosmetic (REQ-NFR-011).
- **REQ-MOD-006 (MUST)**: the embedding model is versioned and **frozen per corpus**
  (`embedding_model_version` in the metadata of each chunk). Changing the
  embedding model requires complete re-indexing: migration is done via **double
  indexing** (old + new index in parallel, switch after validation of retrieval evals,
  then deletion of the old one). Mixing two embedding spaces in a single index is forbidden.

## 3. Reference Portfolio (July 2026 — to be revalidated)

| Class | Role | Candidates | Size / Arch | License |
|---|---|---|---|---|
| **XS** | classification, routing, guardrails, query rewriting | Qwen3-4B/8B, Gemma-class, Phi-4 | dense, 4–15B | Apache/MIT |
| **S** | simple chat, extraction, summarization | Qwen3-30B-A3B (MoE) | ~30B total / 3B active | Apache-2.0 |
| **M** | default conversational, RAG | Qwen3-235B-A22B | 235B total / 22B active, ctx 1M | Apache-2.0 |
| **L** | reasoning, code, long-horizon agentic | GLM-5.x, Kimi K2.5/K2.6, DeepSeek | MoE ~750B–1T total / 32–40B active | MIT / Modified MIT |
| **Embeddings** | RAG | open-weight multilingual model, dim ≤ 1024 | — | Apache |
| **Reranker** | RAG | open-weight cross-encoder | — | Apache |

Engineering notes:
- **MoE** (few active parameters per token) are the decisive element for cost:
  a 235B-A22B costs approximately the same at inference as a dense ~22–30B, while
  retaining the capacity of a very large model. **Systematically prefer MoE.**
- L models (≈1T total parameters) require 8×H100/H200 nodes. Do not
  self-host them before phase 3 (cf. `04` §4).
- Long context (200k–1M) is available but **expensive**: every input token is
  paid for. Well-implemented RAG remains cheaper than "putting everything in the context".
  REQ-MOD-004 (SHOULD): cap the served context at 32k tokens by default; beyond that,
  use hierarchical synthesis or RAG.

## 4. Deployment Modes — decision by phase

| Mode | When | Advantages | Disadvantages |
|---|---|---|---|
| **A. Serverless open-weight (per token)** at an EU provider | **Phase 1 and 2** | Zero capex, elasticity, no GPU ops | Higher €/token at high load; dependency; requires ZDR clause |
| **B. Managed dedicated endpoint** (reserved GPUs at provider) | Phase 2–3, stable load | Predictable perf, stable prefix cache | Hourly billing even when idle |
| **C. Self-hosting** (our GPUs, vLLM/SGLang) | Phase 3+, if threshold `04` §4 crossed or absolute sovereignty constraint | Lowest marginal cost at high utilization; total control | Heavy ops (drivers, failures, capacity planning), 24/7 SRE |

- **REQ-INF-001 (MUST)**: regardless of the mode, the interface remains that of the
  `model-gateway`. The transition A→B→C must have **no application impact**.
- **REQ-INF-002 (MUST)**: provider contract with **zero retention**, EU hosting,
  and prohibition of training on our data (REQ-NFR-006/007). Without these clauses,
  the provider is disqualified, regardless of price.
- **REQ-INF-014 (MUST)**: beyond data protection, the provider contract covers: **SLA**
  for availability and latency with penalties; **quota guarantees**
  (contractual rate limits, increase procedure); **notice on changes**
  in pricing (≥ 60 days) and models (deprecation ≥ 90 days); **reversibility plan** (export,
  end of contract). These clauses are verified by legal before qualification, just like the ZDR. The fallback (REQ-INF-004) provides technical protection; this contract provides economic protection.

## 5. Routing and Cascade — the main cost lever

`model-gateway` implements a **cascade**:

```
request → XS classifier (cost ~€0.00001)
        → estimates: complexity, tool need, reasoning need
        → routes to S / M / L
        → if model S produces a response whose confidence (XS judge) < threshold
          → escalate to M, then L (max one escalation per request)
```

- REQ-INF-003 (MUST): the routing classifier is itself evaluated (`09`); its confusion
  matrix is monitored. An error "L classified as S" (under-routing) is much more costly
  in quality than the reverse error in €. Optimize the threshold based on this asymmetry.
- REQ-INF-004 (MUST): each task class has a **default** model and a **fallback**
  at another provider, declared in config:

```yaml
# config/routing.yaml — source of truth, versioned
task_classes:
  chat_simple:
    primary:  { provider: prov_a, model: model_s, quant: fp8 }
    fallback: { provider: prov_b, model: model_s_alt }
    max_cost_eur_per_call: 0.002
  reasoning:
    primary:  { provider: prov_a, model: model_l }
    fallback: { provider: prov_b, model: model_l_alt }
    max_cost_eur_per_call: 0.05
```
- REQ-INF-005 (SHOULD): *speculative decoding* (draft model XS + verification by the
  target model) in self-hosting — typical gain 1.5–2.5× on latency, without quality loss (the output remains exactly that of the target model).

## 6. Self-hosting — technical specification (to activate in phase 3)

- REQ-INF-006 (MUST): server = **vLLM** or **SGLang**. No "naive" inference
  (`transformers.generate` in prod is forbidden).
  *Watchlist (not a prod option to date)*: **ZML** (Apache-2.0, Zig/MLIR) — compiled
  inference stack aiming for hardware decoupling (NVIDIA/AMD/TPU/Trainium), aligned
  with our anti-lock-in logic but at a lower level. Entry criteria to open an ADR: (a) support for our M/L model classes (MoE), (b) OpenAI-compatible server with continuous batching **and** prefix caching, (c) a demonstrated hardware cost differential (e.g., AMD MI3xx at −30% vs NVIDIA at equal throughput), (d) operational maturity (releases, adoption). Review at the quarterly `model-review` ritual. Thanks to REQ-ARC-006, future adoption would only impact this component.
- REQ-INF-007 (MUST): enable *continuous batching*, *paged attention*, and
  **automatic prefix caching**. With a system prompt + long and shared tool definitions, prefix caching massively reduces the prefill cost (A-5: > 70% hit rate expected). This is the first setting to verify, before any other optimization.
- REQ-INF-008 (MUST): **FP8** quantization (weights + KV cache) by default; INT4/AWQ
  only if an eval demonstrates a loss < 1 point on our tasks.
- REQ-INF-009 (MUST): parallelism — intra-node tensor parallel, expert parallel for
  MoE; do not cross the node boundary without interconnect (NVLink/InfiniBand).
- REQ-INF-010 (MUST): autoscaling on queue depth, **not** on GPU
  utilization rate (which is misleading: a GPU can be at 100% waiting on memory).
- REQ-INF-011 (SHOULD): separate **interactive** pools (low batch, latency) and
  **batch** pools (large batch, spot instances, up to −70% cost, eviction-tolerant).
- REQ-INF-012 (MUST): load weights from a local/NVMe cache or an internal artifact
  registry — no downloading from the Internet at pod startup (startup time + supply chain risk).
- REQ-INF-013 (MUST): verify the fingerprint (checksum/signature) of weights; downloaded models are a supply chain vector. Internal mirroring is mandatory.

### Capacity planning — formulas to use (do not guess)

```
VRAM ≈ quantized_weights + KV_cache + activations + overhead(~10%)

KV_cache_per_token ≈ 2 × n_layers × n_kv_heads × head_dim × bytes_per_element
KV_cache_total     ≈ KV_cache_per_token × average_context × concurrent_requests
```
The KV cache, not the weights, is what limits concurrency in practice. Recent models reduce its footprint (sparse attention, sliding windows, GQA/MLA): this is a **selection criterion in its own right** for self-hosting, often more decisive than 2 benchmark points.

## 7. Acceptance Criteria

- AC-INF-1: Reproducible load benchmark (versioned script) producing TTFT, TPOT,
  throughput, cost/1k req, for each candidate configuration.
- AC-INF-2: Prefix cache hit rate is exposed as a metric and > 60% in prod.
- AC-INF-3: Provider switch tested and timed < 1 h (REQ-NFR-009).
- AC-INF-4: The routing cascade reduces cost/request by at least 40% vs "all on L",
  at an eval quality ≥ 97% of "all on L".
