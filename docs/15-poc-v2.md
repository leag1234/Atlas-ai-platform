# PoC v2 — Credible, Sovereign Test Bench

> Subject: expand PoC v1 (proven foundation) into a **test bench** that resembles
> a frontier assistant closely enough to honestly evaluate foundational quality before
> any product investment. **This is NOT the final product**: the UI remains basic,
> multi-user robustness, hardened security, and polish are out of scope.
> Unchanged constraint: **100% sovereign** (everything on Scaleway infrastructure / GPU
> rented in the EU; no non-European proprietary runtime in the execution path).

## Guiding Principle

**Approach the behavior of a frontier, never dodge.** Whenever a capability is reasonably
expected, the system provides it to the best of its ability rather than returning an
error message or an excuse. Concretely: we do not "truncate/reject" a large input, we
**synthesize** it; we do not say "I cannot see images", we **analyze** them; etc. A
failure message is acceptable only for a real impossibility (resource unavailable,
budget exceeded), never to avoid expected work.

## Interaction Confidentiality (ABSOLUTE RULE)

Interaction logs (`BRAIN/interactions/*.jsonl`) contain real requests, potentially
personal. They are **strictly local to the VM**:
- never committed (already in `.gitignore`; re-verify at each milestone);
- never summarized, quoted, or mentioned in a versioned file, a PR, a report,
  or a message;
- observability analysis produces **anonymous aggregated metrics** (latencies,
  failure rates, routing distribution, error types) — never the content nor
  excerpts of requests.
Any violation of this rule is a security incident (immediate halt).

## Milestones

### M8 — Serverless Model (selection and configuration)
**Architecture decision (PoC v2)**: EVERYTHING on Scaleway Generative APIs serverless.
No local GPU for text inference: at low volume, serverless is cheaper (pay-per-token,
~€0.006/request) and provides access to large models that we cannot host ourselves.
The GPU is rented only OCCASIONALLY for image generation (M13). Sovereign self-hosting
is a PRODUCTION decision to be made after the PoC, depending on volume (profitable
beyond ~50M tokens/month) and required quality level.
**Deliverables**: configuration of serverless models: generalist (qwen3.5-397b),
code (glm-5.2 or qwen3-coder), vision (pixtral-12b), embeddings (qwen3-embedding-8b);
context window and function-calling verified; the M4 router becomes a routing by
TASK TYPE (text/code/vision) between serverless models instead of a local/escalade cascade.
The M4 fallback is preserved between serverless models.
**Validation**: each task type is served by the intended model; function calling
and streaming work on the main model; cost per request is traced.

### M9 — Memory + Projects
**Goal**: consolidated context shared between conversations (the largest gap for real usage).
**Deliverables**:
- concept of **project** (groups conversations + ingested documents + notes);
- persisted **conversation memory** (the assistant remembers past exchanges within
  the same conversation and the same project);
- **consolidation**: summaries/facts persisted per project, injected into the context
  of project requests (via existing RAG, extended to project context);
- UI: project selection, memory visible/erasable (basic, unpolished).
**Architecture (Claude/ChatGPT model), two layers**:
- **Project**: space grouping conversations + ingested documents + permanent
  instructions; documentary context shared between all conversations in the project
  (extension of M2 RAG to a project scope).
- **Fact memory**: AUTOMATIC extraction of durable and salient facts from exchanges
  (preferences, work context, decisions), stored separately, **editable and erasable
  by the user**, reinjected into context when relevant. NO raw summary nor reinjection
  of the entire history: selected facts, as done by Claude (memory) and ChatGPT (memory).
**Frontier principle**: the system exploits accumulated context without having to repeat
everything; it clearly distinguishes "this comes from your memory" from "this comes from
a source". Memory is inspectable and correctable by the user.
**Validation**: information given in one conversation of a project is reused in another
conversation of the same project; memory erasure is effective; isolation between projects
(no context leakage from one project to another).
The project chat reserves the M8 serverless configuration and selects scoped passages
under the same context budget; the eight results remain observable.

### M10 — Large Inputs Handled by Synthesis
**Goal**: absorb large inputs (bulky web pages, long documents) like a frontier, via
hierarchical synthesis / retrieval, **never by truncation-excuse**.
**Deliverables**:
- for web content: chunking + selection of relevant passages (retrieval on fetched
  content) OR hierarchical summary before generation;
- for long documents: same logic;
- proper handling of the model's context ceiling (REQ-MOD-004), with a bounded
  synthesis budget.
**Frontier principle**: a query on bulky content produces a useful and sourced answer,
not a "context exceeded" message.
**Validation**: a question on a long web page (which caused PoC v1 to fail) produces
a correct and sourced answer, within the time/cost budget.

### M11 — Streaming + Visible Reasoning
**Goal**: responses displayed as they arrive; reasoning trace for long answers.
**Deliverables**:
- the adapter relays the SSE stream token-by-token to the UI (end of "block after wait");
- if the model exposes a reasoning trace (reasoning_effort), display it separately
  when relevant, with the token overhead signaled/bounded.
**Evaluation target**: perceived latency strongly reduced; feel close to a frontier.
**Validation**: tokens display progressively; budget per request is maintained.
The SSE transport applies the M8 serverless policy before I/O; a fallback remains
possible before any visible fragment, never after emitting content/reasoning.

The client also relays project responses progressively: only the response field is
displayed; the consolidation JSON remains internal and the final response is validated.

Streamed citations use retrieved passages and routes specific to the project; the
memory preamble remains identical between JSON and SSE.
Contract: `contracts/m11.md`; protocol and limits: `reports/M11.md`.
`make verify-m11` verifies progression with a provider barrier on real HTTP.

### M12 — Image Description (multimodal input)
**Goal**: the system sees and analyzes sent images.
**Deliverables**: routing of requests containing an image to a sovereign vision model
(pixtral-12b, Scaleway); integration into the gateway and UI (image upload).
**Frontier principle**: an image + a question produces a relevant answer about the
image content, not "I do not process images".
**Validation**: correct description of a test image; answer to a question regarding
its content.
Implementation and measurements: `contracts/m12.md`, `reports/M12.md`; `make verify-m12`
exercises the HTTP path with transport recorded in CI, renewable in record mode.

### M13 — Image Generation (local on GPU, sovereign)
**Goal**: produce images from a description, **locally on GPU**.
**Deliverables**:
- selection of an open-weight image generation model (Flux / SDXL) and its server
  (diffusers/ComfyUI), hosted on the **already rented GPU** (shared with vLLM).
  For a sequential usage test bench (single user, no simultaneous chat + image),
  VRAM sharing is acceptable: SDXL (~12 GB) coexists with the text model on an
  L40S 48 GB. **Selected model: Flux** (quality close to mainstream references).
  As Flux is heavy on VRAM, plan a **sequential switch** on the shared GPU: unload/
  sleep the text model during image generation, then reload (acceptable in single-user
  test bench). Dedicated GPU reserved for the product. The same GPU may carry an STT
  later;
- integration: a generation request routes to this service; the image returns in the UI;
- safeguards: GPU budget, shutdown, no illicit content (minimal filter).
**Constraint**: 100% local/sovereign — no external image generation service.
**Validation**: a request "generate an image of X" produces a coherent image, served
by the local GPU, within budget.

## Organization in Two Phases

**Phase A — the assistant**: M8 (serverless), M9 (memory/projects), M10 (synthesis),
M11 (streaming/reasoning), M12 (vision), M13 (image generation).
→ **INTERMEDIATE TEST**: the user re-tests the complete assistant; analysis of
interaction logs (aggregated metrics, never content); corrections.

**Phase B — integrations**: M14 (MCP client), M15 (hardened OpenAI-compatible API).
→ Final test.

### M14 — MCP Client (act on company tools)
**Goal**: the assistant connects to MCP servers (GitLab, WordPress, etc.) and can
read/act on them (list/create issues, publish, search...).
**Deliverables**: MCP client in the tool harness (extension of M3); configuration of
authorized MCP servers; side-effect actions (create, publish, modify) go through user
confirmation; action logging.
**Frontier principle**: the assistant chains reading + action on tools like a collaborator,
with confirmation before any side effect.
The contract `contracts/m14.md` sets the stdio transport and authorized configuration;
writes open a local form on port8020 (existing SSH tunnel), with preview and single
confirmation. Pending actions expire after 10 minutes or upon process restart; no confirmation
POST is exposed to the model.
**Validation**: reading a GitLab resource via MCP; creating an issue after confirmation;
refusal of an unconfirmed action; no MCP secret exposed.

### M15 — Hardened OpenAI-Compatible API (for developers)
**Goal**: expose the assistant as a model provider usable by Codex, Claude Code, and
any OpenAI client, so developers can test it in their tools.
**Deliverables**: hardening of the M7 adapter: API key authentication, multi-user
(keys/quotas per dev), complete and compliant function-calling and streaming, large
context window (dev tools send entire repositories), code model by default. Codex /
Claude Code integration documentation.
**Validation**: Codex AND Claude Code configured on the API perform an end-to-end code
task; an invalid key is refused; quotas applied; cost per key traced.

## What Remains OUT of Scope (product, later)
Polished/ergonomic UI, quality voice recognition, multi-user chat UI,
authentication/HTTPS/public exposure, high availability, production-hardened security,
complete SRE observability, human calibration of the judge.

> Note STT (product phase): an offline real-time speech recognition component, already
> proven on /e/OS (public repos), can be integrated in the product phase on the same GPU.
> Technical choice validated on the /e/OS side: **Parakeet TDT (ONNX) via transcribe-rs**,
> real-time streaming, preferred over Whisper. Out of scope for PoC v2.

## Method (unchanged since v1)
Specs first; the agent implements under contract + CI; `verify-mN` tested upstream;
milestones merged via PR with green CI; auto-merge allowed on green CI; minor
contradictions resolved autonomously, security/budget = halt. Local interaction logs
only (rule above).

M8: the contract `contracts/m8.md` specifies serverless routing. The policy in the
gateway validates rates and capabilities before transport and reserves primary + fallback
under €0.05. Provider windows are bounded by this application budget; alternative
identifiers require a validated capacity and price entry. Chat without GPU retains the
delay and reserve in case of fallback without known usage. Logs distinguish measured cost
and unknown reserve. The RAG context selects the best-ranked entire passages under 4 KiB;
retrieval@8 remains unchanged. The gate `make verify-m8` and `reports/M8.md` document
synthetic probes and their limits, notably color errors on entirely uniform images.

M13: the contract `contracts/m13.md` defines the chat image branch. Explicit FR/EN
requests go through `/images/generate` on the gateway; PNG output integrated into
Markdown/SSE and masked in logs. The GPU address is configured by `ATLAS_IMAGE_GPU_IP`,
its real rate by `ATLAS_IMAGE_GPU_EUR_H`; reservation 90 seconds at most €0.05. The GPU
deployment is subject to a separate cycle.

M15: contract `contracts/m15.md`, OpenAPI `contracts/m15.openapi.json` and ADR-0001;
local SQLite keys/quotas in the developer entry8030, independent of chat8020 data.
