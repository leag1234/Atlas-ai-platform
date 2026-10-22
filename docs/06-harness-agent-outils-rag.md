# 06 — Harness: Agentic Orchestration, Tools, RAG, Memory

> This is where the perceived difference between "an open-source chatbot" and "a quality experience" is made. An average model with an excellent harness beats an excellent model with a mediocre harness on real-world tasks.

## 1. Orchestrator

### 1.1 State Machine (reminder `02` §2.2)
Explicit, testable, incident recovery. Implicit unbounded loops are forbidden.

- REQ-HAR-001 (MUST): `max_tool_calls` (12), `max_wall_clock` (120 s), `max_cost_eur`
  (0.10 €), `max_tokens` per request. Exceeding limits → **explicit** termination with an honest
  user message ("I could not finish, here is where I stand"), never a silently truncated output.
- REQ-HAR-002 (MUST): loop detection — if the last 3 tool calls are
  identical (same name, same normalized arguments), interrupt and change strategy or explicitly abort.
- REQ-HAR-003 (MUST): parallelization of **independent** tool calls (the model
  can emit several per turn). Major latency impact.
- REQ-HAR-004 (MUST): idempotence — each `step_id` is replayable without duplicated side effects
  (idempotency key propagated to mutating tools).

### 1.2 Context Management
- REQ-HAR-005 (MUST): deterministic compaction policy when the conversation exceeds
  the budget: structured summary of old turns (via cheap S model) + full preservation
  of the last N turns + **full** preservation of recent tool results. The summary is persisted, not recalculated at every turn.
- REQ-HAR-006 (MUST): large tool results (SQL dumps, large files) are
  **truncated with a handle** (`result_id` retrievable via a `fetch_result` tool),
  never pasted in full into the context.

## 2. Tools (function calling)

- REQ-TOOL-001 (MUST): each tool is declared via a strict **JSON Schema**, with
  description, examples, side effects (`readonly` | `mutating` | `destructive`), and
  expected cost/latency.
- REQ-TOOL-002 (MUST): schema validation **before** execution; in case of invalid
  arguments, return a structured error readable by the model ("field `date`: expected format YYYY-MM-DD, received '12 March'") — not a stack trace. The quality of
  error messages determines the model's ability to self-correct: this is a quality component, not plumbing.
- REQ-TOOL-003 (MUST): `destructive` tools require **explicit user confirmation
  outside the model's control** (the UI asks, the model cannot bypass).
- REQ-TOOL-004 (MUST): timeout and retry (exponential backoff, max 2 attempts) per tool;
  a failing tool returns a usable error, it does not fail the entire request.
- REQ-TOOL-005 (MUST): **tool budget per tenant** and logging of each call
  (who, what, when, arguments, truncated result, cost).
- REQ-TOOL-006 (SHOULD): beyond ~20 tools, do not inject all of them into the prompt:
  **dynamic selection** of relevant tools (semantic search on descriptions).
  Warning: this breaks the prefix cache — measure the cost/quality trade-off.

### 2.1 MCP (Model Context Protocol)
- REQ-TOOL-007 (SHOULD): business connectors are exposed via MCP → standardization,
  reusability, decoupling. Internal MCP servers, never unaudited third-party servers.
- REQ-TOOL-008 (MUST): a third-party MCP server is treated as **untrusted code**:
  code audit, version pinning, isolated execution, minimal permissions. Tool
  descriptions provided by an MCP are a prompt injection vector (`07`).

### 2.2 Web Search Tool (REQ-FUN-011, phase 2)
- REQ-TOOL-012 (MUST): search and page retrieval go through a **single egress
  proxy**: domain allowlist/denylist per tenant, logging, request budget, no cookies nor credentials, identified User-Agent.
- REQ-TOOL-013 (MUST): all web content is **untrusted** (REQ-SEC-011) and triggers
  the "restricted privileges" mode for the turn (REQ-SEC-015): no mutating tool in the same
  turn without human confirmation.
- REQ-TOOL-014 (MUST): responses based on the web carry resolvable citations
  (URL + consultation date), with the same post-hoc verification as RAG
  (REQ-RAG-011). Short excerpts only: no substantial reproduction of third-party
  content (copyright — same rule as for internally licensed documents).
- REQ-TOOL-015 (SHOULD): page cache (TTL by content type) for cost and trace reproducibility.

### 2.3 Code Execution Sandbox
- REQ-TOOL-009 (MUST): isolation via microVM (Firecracker) or gVisor. **Not** a simple
  Docker container.
- REQ-TOOL-010 (MUST): no outbound network by default; explicit allowlist if needed.
  CPU/RAM/disk/duration limits. Ephemeral file system, destroyed after use.
- REQ-TOOL-011 (MUST): no secrets, no production environment variables in
  the sandbox.

## 3. RAG — Specification

### 3.1 Ingestion
```
source → extraction → cleaning → chunking → enrichment → embedding → index
```
- REQ-RAG-001 (MUST): faithful extraction by type (text PDF vs scanned PDF → OCR; tables
  preserved in structure, not flattened into word soup; docx → preserve titles/hierarchy).
  Sloppy extraction is **the primary cause** of poor RAG — long before the choice of the
  embedding model.
- REQ-RAG-002 (MUST): semantic chunking respecting structure (titles, sections),
  target size 300–800 tokens, overlap 10–15 %. Each chunk carries its **parent
  context** (document title, section path) prefixed — significant recall gain for
  zero cost.
- REQ-RAG-003 (MUST): mandatory metadata per chunk: `tenant_id`, `doc_id`,
  `chunk_id`, `source_uri`, `acl`, `version`, `date_maj`, `checksum`.
- REQ-RAG-004 (MUST): incremental re-indexing on source change; effective deletion
  when the source is deleted (right to erasure, GDPR).

### 3.2 Search
- REQ-RAG-005 (MUST): **hybrid search** — BM25 (lexical) + dense (semantic), fusion
  via RRF. Lexical alone misses paraphrases; dense alone misses identifiers,
  product codes, legal references. In enterprise, lexical is indispensable.
- REQ-RAG-006 (MUST): **cross-encoder reranker** on top-50 → top-5/8. This is the
  best gain/effort ratio in the entire RAG pipeline.
- REQ-RAG-007 (MUST): **ACL filtering at the query level**, not after. The index is
  queried with the user's permission context. A chunk a user has no right to see must never reach the reranking layer.
  Mandatory non-regression test (`08` §4).
- REQ-RAG-008 (SHOULD): query rewriting (XS model): decontextualization of
  pronouns from history, multi-query expansion. Notable recall gain.
- REQ-RAG-009 (SHOULD): *self-check* — if no chunk exceeds a relevance threshold,
  the model **must** answer "I did not find information on this subject in your documents" rather than improvising. This rule alone eliminates a large part of perceived hallucinations.

### 3.3 Generation and Citations
- REQ-RAG-010 (MUST): every factual assertion derived from documents carries a citation
  `chunk_id` resolvable to a clickable source.
- REQ-RAG-011 (MUST): **post-hoc verification of citations** — a verifier
  (XS model or NLI) checks that each cited sentence is actually supported by the
  referenced chunk. Unsupported citations are removed and the incident is measured
  (metric `citation_faithfulness`, cf. `09`).
- REQ-RAG-012 (MUST): retrieved content is framed as **untrusted data**
  (`07` §3) — never interpreted as instructions.

## 4. Memory

Three horizons, not to be confused:
1. **Current conversation context** (§1.2).
2. **Long conversation memory**: persisted summaries, recalled upon loading.
3. **Long-term memory / user facts**: REQ-HAR-007 (SHOULD) — extraction of
   stable facts, explicit storage, **visible and editable by the user**
   (transparency, GDPR). Opt-in. Never implicit.

- REQ-HAR-008 (MUST): memory is segregated by `tenant_id` **and** by `user_id`.
  An inter-user memory leak is a severity 1 security incident.

## 5. Acceptance Criteria

- AC-HAR-1: integration test proving termination on each of the 4 budgets (REQ-HAR-001).
- AC-HAR-2: test proving that a user without rights to a document never obtains
  that document, even via indirect questioning (minimum 10 adversarial scenarios).
- AC-RAG-1: evaluation of retrieval isolated from the LLM (recall@k, MRR, nDCG) on a golden
  set of ≥ 200 question/document pairs. One does not debug RAG by looking at final
  answers: one measures each stage separately.
- AC-RAG-2: `citation_faithfulness` ≥ 0.95 on the golden set.
- AC-TOOL-1: tool-calling success rate ≥ 95 % on a suite of 100 scenarios
  (good arguments, correct tool, good recovery after injected error).
