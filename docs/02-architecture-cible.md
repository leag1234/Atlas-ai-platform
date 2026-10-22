# 02 — Target Architecture

## 1. Overview (C4 Level 2)

```
                    ┌──────────────┐
   Web / Desktop ───│  edge-bff    │  auth OIDC, rate-limit, SSE
   Mobile           └──────┬───────┘
                           │ gRPC/HTTP (OpenAPI v1 contract)
                    ┌──────▼───────────────────────────┐
                    │        orchestrator              │  agent loop,
                    │  (state machine, no hardcoded    │  token/time budget,
                    │   LLM logic)                     │  failure recovery
                    └─┬────┬────┬────┬────────────┬────┘
                      │    │    │    │            │
        ┌─────────────▼┐ ┌─▼──────┐ ┌▼─────────┐ ┌▼──────────────┐
        │ model-gateway│ │ guard  │ │ retrieval│ │ tool-runtime  │
        │ (routing,    │ │ rails  │ │ (RAG)    │ │ (MCP, sandbox)│
        │  fallback,   │ │        │ │          │ │               │
        │  cache)      │ └────────┘ └────┬─────┘ └───────┬───────┘
        └──┬───────┬───┘                 │               │
           │       │              ┌──────▼─────┐   ┌─────▼──────┐
   ┌───────▼──┐ ┌──▼────────┐     │ vector +   │   │ gVisor /   │
   │ provider │ │ self-host │     │ BM25 + doc │   │ Firecracker│
   │ serverless│ │ vLLM/SGLang│    │ store      │   │ sandbox    │
   └──────────┘ └───────────┘     └────────────┘   └────────────┘

  Transverse : identity, config (feature flags), observability (OTel),
               eval-harness, audit-log (append-only), finops-collector
```

## 2. Components — Single Responsibility

### 2.1 `edge-bff`
- TLS termination, OIDC/SAML authentication, tenant-based authorization, quotas.
- SSE streaming to the client; **no business logic**.
- REQ-ARC-001 (MUST): The BFF never speaks directly to a model provider.

### 2.2 `orchestrator` — System Core
Explicit state machine, **not** an implicit `while true` loop.

States: `PLAN → RETRIEVE? → GENERATE → TOOL_CALL? → OBSERVE → GENERATE … → FINALIZE`

- REQ-ARC-002 (MUST): Every transition is logged with a `trace_id` and a `step_id`.
- REQ-ARC-003 (MUST): Hard budgets per request — `max_tokens`, `max_tool_calls` (default 12),
  `max_wall_clock` (default 120 s), `max_cost_eur` (default €0.10). Exceeding limits triggers a clean
  stop with a user message; never silent truncation.
- REQ-ARC-004 (MUST): Conversation state is persisted after each step → recovery
  possible after a crash (idempotence via `step_id`).
- REQ-ARC-005 (MUST): No hardcoded prompts in the code. Prompts are **versioned
  artifacts** (`prompts/<name>/<semver>.md`), loaded by identifier, and tested in evaluations.

### 2.3 `model-gateway` — Critical Indirection Point
The only component aware of the existence of models and providers.

- REQ-ARC-006 (MUST): Exposes a stable and neutral internal API (`POST /v1/generate`,
  OpenAI-compatible schema internally — this is the ecosystem's lingua franca).
- REQ-ARC-007 (MUST): Routing by **task class**, not by model name.
  The caller requests `task_class: "chat_simple" | "reasoning" | "coding" | "extraction"
  | "classification" | "summarize"`. The class→model mapping is in config.
- REQ-ARC-008 (MUST): Cascading fallback and *circuit breaker* per provider.
- REQ-ARC-009 (MUST): Semantic cache + exact cache (see `04-finops.md` §5).
- REQ-ARC-010 (MUST): Counts tokens and cost per request, tenant, and feature.
- REQ-ARC-011 (SHOULD): *shadow traffic* — ability to send X% of traffic to a
  candidate model without serving its response, for offline comparison.

**Contract (excerpt, to be frozen in OpenAPI before any code):**
```yaml
POST /internal/v1/generate
request:
  task_class: string        # required
  messages: [{role, content, tool_calls?, tool_call_id?}]
  tools?: [JSONSchema]
  constraints: {max_tokens, temperature, stop?, response_format?}
  routing_hints?: {latency_class: "interactive"|"batch", quality_floor: 0..1}
  tenant_id: string         # required, for quota + isolation
  trace_id: string          # required
response (SSE or unary):
  content_blocks: [{type: "text"|"tool_use", ...}]
  usage: {input_tokens, output_tokens, cached_input_tokens, cost_eur}
  model_meta: {provider, model_id, model_version, routed_by}
```

### 2.4 `guardrails`
Two mandatory passes: **pre-generation** (user input + retrieved content)
and **post-generation** (output). Details: `07-securite-et-conformite.md`.

### 2.5 `retrieval`
Hybrid BM25 + dense + reranker. Details: `06-harness-agent-outils-rag.md`.

### 2.6 `tool-runtime`
Tool execution, including MCP servers. Strong isolation. Details: `06`.

## 3. Architecture Decisions (Condensed ADRs)

| ADR | Decision | Rationale | Consequence |
|---|---|---|---|
| ADR-001 | No dedicated GPU in phase 1; inference via open-weight provider on-demand, hosted in EU. | Volume < break-even threshold (`04` §4). | Provider dependency → mitigated by ADR-002. |
| ADR-002 | `model-gateway` mandatory, neutral contract, ≥ 2 qualified providers at all times. | Avoid lock-in; REQ-NFR-009. | Initial dev cost +2 weeks. |
| ADR-003 | Routing by task class with small→large model cascade. | 60–80% of requests do not need a frontier model. | Requires a complexity classifier (see `03` §5). |
| ADR-004 | Prompts and policies = versioned artifacts, not code. | Fast iteration + reproducible evaluations. | Prompt registry to be built. |
| ADR-005 | Python (ML, harness) + Go or TypeScript (edge services/tooling). One language per service. | ML ecosystem; edge performance. | Two CI pipelines. |
| ADR-006 | Postgres (+ pgvector at startup) rather than a dedicated vector database. | Operational simplicity < 50M chunks. | Migration planned (Qdrant/Vespa) if > 50M or p95 latency > 150 ms. |
| ADR-007 | Kubernetes + GitOps (ArgoCD) + Terraform. No imperative deployment. | Reproducibility, audit. | Learning curve. |
| ADR-008 | No heavy agent framework (LangChain & co) in the critical path. | Hidden debt, leaky abstractions, hard to test. | We write ~1,500 lines of explicit orchestrator. Accepted. |

> ADR-008 is important for implementer agents: the temptation to import a
> framework to "go fast" produces a system impossible to evaluate and debug.
> Libraries are allowed **outside** the critical path (ingestion, notebooks).

## 4. Reference Flow — RAG Request with Tool

1. `edge-bff`: auth → quota → creates `trace_id` → opens SSE stream.
2. `orchestrator`: loads conversation state, applies budget.
3. `guardrails.pre(input)` → if blocked, templated refusal response, end.
4. `retrieval.search(query, tenant_id)` → top-k chunks + scores. **Chunks are marked
   `untrusted`** and framed in the prompt (anti-injection, cf. `07`).
5. `model-gateway.generate(task_class=…)` → stream.
6. If `tool_use`: `tool-runtime.execute()` (result also marked `untrusted`) → return to 5.
7. `guardrails.post(output)` → if blocked, do not display; incident logged.
8. Persistence: messages, usage, cost, citations, versions (model, prompt, policy).

## 5. Environments

`dev` (ephemeral per branch) · `staging` (prod mirror, synthetic data) ·
`prod`. No direct human write access to `prod`: everything goes through GitOps.

## 6. Architecture Acceptance Criteria

- AC-ARC-1: Changing inference provider = 1 config PR, 0 application code changes. **Tested** by a monthly game day.
- AC-ARC-2: An integration test proves that exceeding `max_cost_eur` interrupts the request.
- AC-ARC-3: A single OTel trace covers the full path BFF → gateway → provider.
- AC-ARC-4: `grep -r "qwen\|glm\|deepseek\|llama" --exclude-dir=model-gateway src/` returns nothing (blocking CI).
