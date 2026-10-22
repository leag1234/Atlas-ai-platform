# 01 — Requirements and Scope

## 1. Context and Load Assumptions

Assumptions to be **validated within the first 2 weeks** (they drive all FinOps).
As long as they are not measured, they are marked `ASSUMPTION` and must not
serve as a basis for a GPU purchase commitment.

| Id | Assumption | Initial Value | Validation Method |
|---|---|---|---|
| A-1 | Active internal users (DAU) | 200 → 2,000 | 4-week pilot |
| A-2 | Requests / user / day | 25 | Telemetry |
| A-3 | Input tokens / request (with RAG) | 6,000 | Telemetry |
| A-4 | Output tokens / request | 700 | Telemetry |
| A-5 | Prefix reuse rate (system + tools) | > 70% | vLLM metric `prefix_cache_hit_rate` |
| A-6 | Peak / average | 4× | Telemetry |

Derived volume (200 DAU): ≈ 5,000 req/day → **~34 Mtok input / 3.5 Mtok output per day**.
At 2,000 DAU: ~340 Mtok input / 35 Mtok output per day.
> This volume is **well below** the break-even threshold for a dedicated GPU cluster
> (cf. `04-finops.md` §4). Major architectural consequence: phase 1 = **inference
> delegated to a pay-per-use open-weight provider**, no purchased GPUs.

## 2. Functional Requirements

| Id | Requirement | Level |
|---|---|---|
| REQ-FUN-001 | Multi-turn conversation with token-by-token streaming. | MUST |
| REQ-FUN-002 | Tool calling (function calling) with server-side execution, parallelizable. | MUST |
| REQ-FUN-003 | RAG on corporate document bases with verifiable citations (every sourced statement points to a retrievable `chunk_id`). | MUST |
| REQ-FUN-004 | File ingestion (pdf, docx, xlsx, csv, images) and Q/A on them. | MUST |
| REQ-FUN-005 | Code execution in an isolated sandbox (data analysis, file generation). | SHOULD |
| REQ-FUN-006 | Conversation memory: sliding summary + recall of past conversations. | SHOULD |
| REQ-FUN-007 | Workspaces / projects with shared context and ACLs. | SHOULD |
| REQ-FUN-008 | Business connectors (SharePoint/Drive, Jira, Confluence, SQL database) via MCP. | SHOULD |
| REQ-FUN-009 | Assistant customization (system prompt + tools + corpus) per team. | SHOULD |
| REQ-FUN-010 | Multimodal image input. | MAY (phase 3) |
| REQ-FUN-011 | Web search with citations, as a controlled tool (domain allowlist/denylist per tenant, content treated as untrusted, cf. `06` §2.3 and `07` §3). | SHOULD (phase 2) |
| REQ-FUN-012 | Complete export of a tenant's data (conversations, documents, memories) in an open and documented format (GDPR art. 20 portability, reversibility). | MUST |

## 3. Non-Functional Requirements

| Id | Requirement | Target | Verification |
|---|---|---|---|
| REQ-NFR-001 | TTFT (time-to-first-token) p95 | < 1.2 s | Synthetic load in CI |
| REQ-NFR-002 | Inter-token throughput p95 | > 25 tok/s | idem |
| REQ-NFR-003 | Monthly API availability | 99.5% (internal), 99.9% (product) | SLO, cf. `10` |
| REQ-NFR-004 | 5xx error rate | < 0.5% | SLO |
| REQ-NFR-005 | Unit cost | < €0.015 / average request | FinOps Dashboard |
| REQ-NFR-006 | No corporate data leaves the EU | 100% | Contractual control + network |
| REQ-NFR-007 | No customer data used to train a third party | 100% | "Zero data retention" clause |
| REQ-NFR-008 | RTO 4 h / RPO 15 min on conversational data | — | Quarterly restoration test |
| REQ-NFR-009 | Inference provider failover | < 1 h, without application redeployment | Monthly failover test (game day) |
| REQ-NFR-010 | Traceability: every response reconstructible (prompt, model version, tools, docs) | 100% | Audit log |
| REQ-NFR-011 | **Multilingualism with equal treatment**: equivalent quality in FR, DE, ES, IT, and EN (comprehension, generation, and translation between these languages). The evals (`09`) cover each language and scores are reported **per language**; no model is selected based on English scores alone. Tokenizer efficiency in target languages (tokens/word) is included in the cost calculation `04` (15–40% variance possible between languages and between tokenizers). | Inter-language gap ≤ 10% on evals | Eval per language + tokens/word measurement |
| REQ-NFR-012 | UI accessibility: RGAA / WCAG 2.1 AA compliance. | AA | Accessibility audit before GA |

## 4. Security Requirements (summary, details in `07`)

| Id | Requirement | Level |
|---|---|---|
| REQ-SEC-001 | Input/output filtering by classifiers before anything is displayed to the user. | MUST |
| REQ-SEC-002 | Strict data isolation per tenant, including in vector indexes. | MUST |
| REQ-SEC-003 | Defense against indirect prompt injection via documents/tools. | MUST |
| REQ-SEC-004 | Code execution sandbox without outgoing network access by default. | MUST |
| REQ-SEC-005 | Immutable audit log, 12-month retention. | MUST |

## 5. Scope

**In scope**
Multi-provider model gateway · agentic orchestrator · RAG · tools/MCP ·
guardrails · API + UI · evals · observability · FinOps · light post-training (SFT/DPO
on LoRA adapters) starting from phase 2.

**Out of scope**
Pre-training · fundamental alignment research · own datacenter ·
image generation · voice (phase 4 at the earliest).

## 6. Program Success Criteria

- CS-1: ≥ 60% of target users active at 30 days after internal GA.
- CS-2: Human satisfaction score ≥ 4.0/5 on a weekly sample of 100 conversations.
- CS-3: Internal eval suite ≥ 90% of the reference proprietary model score on priority business tasks.
- CS-4: Unit cost compliant with REQ-NFR-005.
- CS-5: Zero inter-tenant data leak incidents.
