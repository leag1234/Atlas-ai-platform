# Archived milestone definitions and decisions

Moved from MISSION.md during M20; historical text below is preserved.

# MISSION — ATLAS-0

Build the PoC defined in `docs/13-poc-spec.md`, milestone by milestone, with each milestone proven by `make verify-mN` **green on GitHub CI** (not just locally).

Golden rule: a milestone only exists when its CI is green. Your word means nothing; the `ci` job means everything. You never modify `.github/workflows/` or `scripts/verify-*` (protected by CODEOWNERS).

The order is **mandatory**. M0 then M1 first: without CI and without reproducible infrastructure, everything else is unverifiable.

---

## M0 — Verifiable Framework
**Goal**: the verification loop runs end-to-end on an empty project.
**Deliverables**: service structure (skeletons), `make` target, CI executing lint+typecheck+tests+secret-scan, `eval-harness` capable of running empty.
**verify-m0** verifies: `make lint test` green; `make eval-smoke` executes (0 cases ok); an `edge-bff` healthcheck responds 200; the secret scan passes; the anti-model-name grep (docs/02 AC-ARC-4) finds nothing outside `services/model-gateway`.

## M1 — Reproducible GPU Infra + Gateway

**Goal**: prove that a GPU node is created by script, serves a model via vLLM, then is destroyed. Deliverables: `infra/gpu-up.sh`, `infra/gpu-down.sh`, config `services/model-gateway/`.

**verify-m1** validates ON THE VM (GPU access + Scaleway creds), NOT on GitHub CI (no GPU nor secrets). On CI: static control of scripts. On the VM, `make verify-m1` runs ONE cycle: creates the node, verifies vLLM responds on /v1/models, launches a control inference, archives a simple bench under BRAIN/bench/, destroys the node, all in under 20 min. The proof of M1 is the successful execution of `make verify-m1` on the VM. See contracts/m1.md.

## M2 — Ingestion + RAG
**Goal**: Hybrid RAG with resolvable citations.
**Deliverables**: ingestion (pdf/docx/md/html) → chunks + metadata + embeddings; BM25+dense+reranker search; generation with `chunk_id` citations.
If `evals/golden/` does not yet contain E1/E2/E3, run `generation/KIT-E1-E2-E3.md` on the provided corpus (status `genere-a-valider`, awaiting human validation).
**verify-m2** verifies on the multilingual corpus (corpus/): ingestion produces chunks with metadata; retrieval achieves **recall@8 ≥ 0.70** on the E1 golden set (M2 gate; target 0.85 eventually, adjustable via M2_RECALL_MIN); every citation in a response points to a chunk actually retrievable. M2 does not create a GPU.

## M3 — Agentic Harness + Web Tools
**Goal**: bounded tool loop, safe web search + reading.
**Deliverables**: state machine (hard budgets POC-P6), tools `web_search` (SerpApi), `web_fetch` (trafilatura, robots.txt, anti-SSRF), `rag_search`, `calculator`.
**verify-m3** verifies: POC-E4 ≥ 90%; SSRF tests (private IPs refused) + robots.txt + fetch ceiling; the 4 budgets trigger a clean stop (integration test); POC-E6 executable end-to-end.

## M4 — Cascade + UI + Escalation
**Goal**: S→L routing, fallback on GPU failure, UI connected.
**Deliverables**: routing classifier, escalation to Scaleway Generative APIs, UI (Open WebUI/LibreChat) connected to the gateway.
**verify-m4** verifies: POC-E8 ≥ 85%, zero under-routing on critical cases; fallback: if the local model is unreachable (simulated failure by cutting the local endpoint, WITHOUT creating a paid GPU), the gateway switches to Scaleway escalation and still responds. The UI (Open WebUI/LibreChat) is a visual PLUS, NON-blocking for this PoC gate.

## M5 — Full Evals + Telemetry
**Goal**: measure, compare, trace.
**Deliverables**: `make eval` (all suites, HTML report with diff + breakdown by language, < 20 min), calibrated judge (POC-R2), cost/latency dashboard, prefix-cache hit exposed.
**verify-m5** verifies: `make eval` complete green and under budget/time; report generated; telemetry metrics present (tokens, cost, TTFT, tok/s, cache hit).

## M6 — Hardening + Final Bench + GO/NO-GO Report
**Goal**: quantified proof for decision making.
**Deliverables**: load bench (POC-P1..P9), minimal input filter, `make demo` (full scenario), GO/NO-GO report auto-generated from measurements.
**verify-m6** verifies: POC-P1..P9 measured and archived; `make demo` runs a RAG + web + escalation journey without error; `reports/GO-NOGO.md` generated with real figures.

---

### Human Checkpoints (outside your responsibility, humans do these)
After M1 (infra/budget), after M3 (10 manual requests), after M5 (judge calibration), after M6 (decision). Between these points, you advance alone and log in BRAIN/.

### M1 — Protection Confirmation (Prerequisite AUTO-2 / POC-I1/I2 lifted)
Budget protections CONFIRMED and ACTIVE: Scaleway alerts at 50% and 80% of €800 (SMS + email), verified in console. GPU shutdown guaranteed by the trap in verify-m1.sh (destruction at end of test no matter what) and by the explicit call to gpu-down.sh. GPU type: automatic selection by gpu-up.sh (L40S-1-48G priority, available). The agent is AUTHORIZED to create a billed GPU to execute make verify-m1. This confirmation must not be requested again.

### M2 — Generation Engine Confirmed
For the RAG GENERATION step (drafting responses with citations), use the L model via Scaleway Generative APIs (ESCALATION_MODEL, already configured in .env, endpoint SCW_GENERATIVE_BASE_URL). The budget ceiling is ACTIVE and confirmed. The small CPU model serves ONLY for embeddings/reranking, not for generation. You are authorized to call Generative APIs to generate and produce reliable citations.

### M3 — Resumption Authorization (provider_error diagnosis)
The provider_error errors on E4-004/013/018 likely come from a truncated response (output token limit too low) when the context is long (fetched web page). You are AUTHORIZED to: (1) increase the gateway output token limit (e.g., 512 -> 2048), (2) truncate/summarize large web contents BEFORE passing them to the model (respecting context ceiling, docs/03 REQ-MOD-004), (3) retry E4/E6 attempts as many times as necessary within the per-request budget limit (€0.05). Scaleway function-calling is supported (official doc verified). Continue until E4 >= 90% then merge. Do not lower the E4 threshold, do not modify correction keys.

### M5 — Reference Judge for Cross-Calibration
The reference judge for cross-calibration is **gpt-oss-120b** (Scaleway Generative APIs, OpenAI family, distinct from the production judge glm-5.2 AND the system under test Qwen — independence of the 3 families is respected). Use the already configured Scaleway endpoint (SCW_GENERATIVE_BASE_URL, same key). Calculate Cohen's κ between glm-5.2 scores and gpt-oss-120b scores on the eval sample. No external access nor human judge required for the PoC; human calibration remains pre-GA.

### M5 — E9 Translation without FLORES (PoC)
Downloading FLORES-200 fails (mirror unavailable/authentication). For the PoC, the E9 suite uses ONLY cases already present in evals/golden/e9_traduction.yaml (written and validated business cases, false friends + terminology). Do NOT run fetch_flores.py, do not block on FLORES. The FLORES-200 extension is a post-PoC action. E9 is evaluated on available 'valide' cases.

### M5 — Calibration Fix (Judge JSON transport)
Scaleway's strict structured-output mode (response_format=json_object / json_schema) produces invalid doubly-encapsulated JSON (prefix `{"{"`). You are AUTHORIZED to:
1. NOT use strict json_schema/json_object mode for judge calls; request JSON in the prompt (text output) and parse it on the client side in a TOLERANT manner (extract the first valid JSON object {...} from the response, ignore potential wrapping). This is NOT "fixing a score": it is transport parsing, the judge's score is never modified.
2. Retry a bounded series of calls (budget < 3 EUR) to produce the calibration.
3. If a judge call remains unparsable after tolerant extraction, exclude it from the κ calculation and SIGNAL it in the report (best-effort calibration on valid cases).
The reference judge remains gpt-oss-120b (family independence preserved).

## M7 — Test UI + Interaction Observability
**Goal**: make the system queryable via a chat UI, with structured and exploitable logging of EVERY interaction, to analyze quality/latency/routing.

**Deliverables**:
- `make serve`: starts all necessary services in one command (http gateway + retrieval + adapter), without GPU by default (everything goes through Scaleway escalation; local GPU is activated only if GPU_LOCAL=1).
- **OpenAI-compatible Adapter**: exposes `POST /v1/chat/completions` (standard OpenAI protocol) and routes it to the internal pipeline (RAG /answer, web search, or escalation depending on request nature). Allows connecting any OpenAI client, including Open WebUI.
- **Open WebUI** in Docker, pointed at the adapter, accessible on port 3000 of the VM. The user opens http://<vm-ip>:3000 and chats with the system.
- **Structured logging per interaction**: each request produces a JSON line in `BRAIN/interactions/<date>.jsonl` with AT LEAST: timestamp, question, réponse, modèle_utilisé (local|escalade), route_decision (simple|complexe), latence_ms (retrieval, génération, total), chunks_récupérés (doc_id + score), citations (resolved chunk_id), tokens (in/out), coût_eur, any errors/timeouts. No secrets nor keys in logs.

**verify-m7** verifies: `make serve` starts services; the adapter responds to a `/v1/chat/completions` request end-to-end (response + citation); a structured log line is produced in BRAIN/interactions/ with required fields; Open WebUI is reachable (HTTP 200 on port 3000). Without GPU (escalation only) by default.

**Out of scope**: multi-user authentication, HTTPS, public exposure (port remains on VM; access via SSH tunnel or direct IP depending on network config).

## PoC v2 (M8→M13) — see docs/15-poc-v2.md

### GitHub Token Incident — ASSUMED RISK (human decision, 2026-09-10)
The GitHub token was exposed outside the VM. The project owner examined the risk and DECIDES not to revoke it for the duration of the PoC: scope limited to a private repo without sensitive data, prototyping context, rotation cost judged higher than residual risk. The incident is thus CLOSED as a blocker: this is not a revocation, it is an explicit and tracked risk acceptance.
Deferred action: rotate token before any production deployment.
The agent MUST NO LONGER block on this point nor ask for confirmation again.

### M13 — GPU Budget Attested (human decision, 2026-09-11)
The Scaleway consumption API is not readable by the project's IAM token; the budget is therefore ATTESTED here by the owner:
- Global monthly budget: €800; consumption to date: < €5; available margin: > €795.
- Hourly GPU ceiling: GPU_MAX_EUR_H=€2.00/h (the L40S is ~€1.47/h).
- GPU spending ceiling for M13: **€30 maximum** for the entire milestone.
- Active Scaleway budget alerts at 50% and 80% (SMS + email).
You are AUTHORIZED to create a GPU for M13 within these limits, provided you shut it down after each test (trap already in place). Do not query the billing API anymore: this attestation stands as proof.

### BUDGET RISK RULE (replaces any prior interpretation) — 2026-09-11
The financial risk related to the GPU is BOUNDED and ACCEPTED by the owner:
- hourly ceiling GPU_MAX_EUR_H = €2/h; M13 ceiling = €30; monthly budget €800;
  SMS+email alerts active at 50% and 80%. Worst case of a GPU forgotten overnight: ~€15.
- This risk is CONSCIOUSLY ASSUMED. A theoretical defect in shutdown (edge case of inventory, unlikely homonym, etc.) IS NOT a reason to stop: it is an improvement note to log in the report, then you CONTINUE.
- You are AUTHORIZED and REQUIRED to provision the GPU for M13, deploy Flux, test, then shut down with infra/gpu.py down. If shutdown returns an error, you log it and signal it — you do not block the milestone before having attempted it.
- Only remaining reasons to STOP: effective breach of ceiling (€30), secret leak, destructive action outside the strike-poc project, modification of a protected file. Preventive audit of shutdown scripts is NOT part of this.
Any new security objection regarding gpu.py/gpu-down must be PROPOSED in the report, never used to block. Provision.

## M16 — English localization of the public repository (PRIORITY)
**Context**: the repository is now PUBLIC. All documentation and code intended for external readers must be in English. This is a blocking presentation issue for an open-source project.

**Scope — TO TRANSLATE into English**:
- `README.md` — **to CREATE** (currently missing): project overview, architecture summary, quickstart (install, `make serve`, tunnel, UI), milestones, licence note. This is the first thing a visitor sees.
- `docs/*.md` (~2200 lines), `contracts/*.md` (~940), `runbooks/*.md` (~150)
- `MISSION.md`, `AGENTS.md`
- All comments, docstrings, log and error messages in `services/`, `scripts/`, `infra/`, `tests/` (~65 files contain French)

**Scope — MUST NOT be translated (French is intentional there)**:
- `corpus/` — the multilingual test corpus (7 languages) is the point of the test set
- `evals/golden/` — questions in FR/DE/ES/IT/AR/ZH are deliberate
- `BRAIN/` — local working journal, not tracked by git

**Constraints**: translation only — do NOT change behaviour, identifiers used by other code, file names, or test semantics. Keep commit messages and future PR titles in English.

**verify-m16** checks: README.md exists and is structured; no French function words in docs/contracts/runbooks/README/MISSION/AGENTS; no French in code comments/messages; and the multilingual corpus and golden sets are still intact (not translated away).

### M16 — Translation method (mandatory)
Do NOT build a segmentation/numbered-transport pipeline for translation. Translate each file DIRECTLY: read the file, produce the English version, write it back, one file at a time. No invariant validation, no concurrent calls, no cache layer. If a file is large, translate it in a few sequential passes over its sections, still writing plain text. Keep markdown structure, code blocks, links and anchors intact. Files to translate, in this order: README.md (create), runbooks/*.md, AGENTS.md, MISSION.md, contracts/*.md, docs/*.md. Commit after each file or small group.

## M17 — Integration: make existing capabilities actually usable (PRIORITY)

**Why this milestone exists.** M8–M15 are all "done": unit tests pass, CI is green,
every `verify-mN` succeeds. Yet a real user session found that **only one capability
out of seven works end to end**. Every `verify-mN` validated *components in isolation*,
never a *user journey* through the chat interface. A capability that answers a direct
function call but is unreachable from the UI is NOT delivered.

**New rule**: a milestone is "done" only when a **user journey through the public chat
API** proves it. Component tests are necessary, never sufficient.

### Defects found in the 2026-09-11 user session (all must be fixed)
1. **Language drift** — reply in English while the conversation was in French, after an
   image was sent. The vision path (likely also web and image paths) does not carry the
   conversation-language rule from `prompts/chat.txt`.
2. **Image generation fails** — a French request to create an image of a dancing dog →
   `stream_error`; UI shows "Uh-oh! There was an issue with the response."
3. **Projects unreachable** — M9 exists in the backend (`services/retrieval/project_api.py`)
   but a user cannot create or use a project from Open WebUI.
4. **Web search never returns a final answer** — UI shows two intermediate tool-call notices then nothing. Tools run; no answer is delivered.
5. **Auxiliary Open WebUI functions always fail** — title/tags/follow-up return
   `provider_error`/`cost`/`request_stopped` on every message. Either make them work
   (dedicated light model + own budget) or disable them with an explicit note.
   Silent permanent failure is not acceptable.
6. **`make serve` is not idempotent** — fails when containers or a previous `serving.py`
   process still exist. It must clean up its own resources first.

### Deliverables
- Fix defects 1–6.
- `tests/journeys/`: automated **user journeys** that call the **public chat API**
  (`POST /v1/chat/completions` on the adapter, exactly as the UI does) and assert on what
  the *user* receives. Internal function calls do not count as journeys.
- `make test-journeys` runs them and writes `BRAIN/eval/journeys.json`.
- **Live vs CI**: journeys needing the web, a provider or a GPU (J4, J6) run **live on the
  VM** and are **replayed from recorded cassettes in CI**. J4 must not create a GPU on
  every run: record once, replay afterwards; a live J4 run is explicit (`JOURNEYS_LIVE=1`).
- `make test-serve-idempotent` (J8) is a **separate** target, never part of the CI gate:
  it restarts the stack and must not run while a user session is active.

### Language criterion (explicit, so it cannot be gamed)
A reply "matches" the question language when a language detector (e.g. `langdetect`,
or a simple French/English function-word ratio) classifies the reply as the same
language as the last user message, with ≥ 0.8 confidence. Code blocks are excluded
from the check.

### Projects: realistic UI path
Open WebUI has no native "project" object. J5 is satisfied by ANY of these, as long as
a user can do it from the UI without editing files: (a) a project exposed as a dedicated
Open WebUI model with an attached knowledge base; (b) a chat command
(`/project create <name>`, `/project use <name>`) handled by the adapter; (c) an Open
WebUI tool/function. Document the chosen path in `runbooks/chat.md`.

### Journeys that must pass
J1 plain question → non-empty final answer, in the question's language.
J2 corpus question → answer with ≥ 1 resolvable citation.
J3 image sent → description, **in the conversation language**.
J4 a French request to generate an image of X → an image is returned (live on VM; cassette in CI).
J5 project → a fact stated in conversation A is used in conversation B of the same
   project; a different project does NOT see it.
J6 web question → tools run **and a final answer is delivered** (live on VM; cassette in CI).
J7 auxiliary calls (title/tags/follow-up) → no error, or explicitly disabled + documented.
J8 `make test-serve-idempotent` → second consecutive `make serve` succeeds (separate target).

**Nice to have (not blocking)**: expose the configured models distinctly (generalist,
code, vision) so Open WebUI's arena can compare them for the quality-evaluation phase.

**Definition of done**: `make verify-m17` passes. The gate itself performs a real call
to the public chat API and checks the reply; it does not trust the report alone.

### M17 — Decision: project-scoped image history (2026-09-11)
The leak you found is real and CORRECT to report: in `chat_pipeline.py`, any image in
the history triggers `process_vision` BEFORE `process_project`, and `vision.py` forwards
every client message to `/vision/complete` without project validation.

DECISION: images must be **scoped to the current project**, not refused.
- Resolve the project scope FIRST, then build the vision payload from messages belonging
  to the current project only. Reorder `process_project` before `process_vision`.
- `vision.py` must receive only in-scope messages; it must never receive history from
  another project (or from outside any project when a project is active).
- Journey J5 (`projects_isolated`) must cover this exact case: image in project Alpha,
  switch to Beta, ask a question → no Alpha content reaches vision. Keep
  `tests/test_project_vision_scope.py` as a permanent regression test.

You are AUTHORISED and REQUIRED to fix this yourself: `chat_pipeline.py` and `vision.py`
are your own, unprotected files. Finding a leak in your own code is not a reason to stop —
it is the work. Only stop if a PROTECTED file would have to change, or if fixing it would
require weakening isolation. Fix it, prove it with J5, then continue M17.


## M18 — Conversation integrity and image fidelity (user-test corrections)

Source: real user session of 2026-09-12. Capabilities now work individually, but the
**conversation breaks as soon as a non-text capability is involved**, and image
generation ignores explicit user constraints. Text-only multi-turn conversation is
confirmed working and must not regress.

### D1 — Follow-up after retrieval is treated as a new request (blocking)
Observed: after a RAG answer on an uploaded PDF, "translate what you just wrote into German" (asked in French) triggered a NEW retrieval ("Retrieved 1 source") then failed.
Required: a follow-up that refers to the assistant's previous answer ("translate that",
"summarise it", "shorter", "in German") must operate on the **previous turn**, not
re-enter retrieval/web/vision routing. Classify follow-ups before routing.

### D2 — `context_exceeded` after image generation (blocking)
Observed: after an image was generated, "you forgot the roller skates" (asked in French) returned
`context_exceeded`.
Cause: the generated image (base64 data URI) is kept in the conversation history sent to
the model. Required: store images **by reference**, never re-inject base64 into the model
context. A user must be able to iterate on a generated image ("add X", "same but Y").

### D3 — Intent ignored when an image is present
Observed: "could you edit this image?" (asked in French) produced an unsolicited description.
Required: the presence of an image must not force description. Read the request:
describe / analyse a specific point / extract text / compare / edit. Editing an existing
image is NOT supported by the current model: say so explicitly and offer what is
possible (describe, or generate a new image from a description). Never answer something
that was not asked.

### D4 — Premature give-up on web search timeout
Observed: a first web attempt timed out; the assistant answered "I could not obtain the
information" and redirected the user to news sites. On a second explicit request it
searched again and found a correct, sourced answer.
Required: at least one automatic retry (different phrasing or source) before giving up.
Only report failure after retries are exhausted, stating what was attempted.

### D5 — Interface labels in a random language
Observed: status labels ("Intermediate step completed: tool call" (displayed in French)) appeared in
French then in German within the same conversation.
Required: interface/status labels are FIXED strings, never produced or translated by the
model. Language of labels follows the UI locale, not the model output.

### D6 — Image generation ignores explicit constraints
Observed: "cat dancing with a dog, roller skates, pink sunglasses" (asked in French)
produced the animals and the pink glasses, no roller skates.
Diagnosis (confirmed in `services/model-gateway/image_worker.py` and `image-model.json`):
`unsloth/FLUX.1-schnell` with `num_inference_steps=4`, `512x512`,
`max_sequence_length=256` (which **TRUNCATES longer prompts** — the roller skates were at
the end of the sentence), fixed `manual_seed(42)` (same prompt always yields the same
image), and no prompt rewriting.

**DECISION: stay on FLUX.1-schnell** (Apache-2.0, freely usable commercially, no gated
download). FLUX.1-dev was considered and rejected for this PoC: it is gated on Hugging
Face and released under a **non-commercial licence**, which would block any production
use. Document this trade-off in `runbooks/`.

Required, within schnell's design:
- `max_sequence_length=512` — stop truncating prompts (primary suspected cause);
- `1024x1024` instead of 512x512;
- `num_inference_steps`: raise to the highest value that still helps on a distilled
  model (schnell is trained for few steps; measure 4 vs 8 and keep the better);
- **random seed** per generation, returned with the image so a user can reproduce one;
- **rewrite the user request into a structured English image prompt** before generation
  (subjects, attributes, scene, style), as mainstream systems do. This is the main lever
  left on a distilled model: log both the original and the rewritten prompt;
- `guidance_scale` stays 0.0: this is schnell's documented setting, NOT a defect;
- raise the worker watchdog from 85 s to 180 s to accommodate 1024x1024.
If, after these changes, explicit constraints are still dropped, report it with evidence
(prompt, rewritten prompt, seed, image) so the human can decide whether the licence cost
of FLUX.1-dev is worth paying. Do not silently accept a truncated prompt.

### Non-regression
Text-only multi-turn conversation, RAG with resolvable citations, web search with a final
sourced answer, vision description in the conversation language, dev API auth/quotas and
MCP confirmation must all keep working.

### Journeys (extend `tests/journeys/`, same rules as M17: public chat API only)
J9  RAG answer, then "translate your answer into German" (asked in French) → German translation of the
    PREVIOUS answer, no new retrieval, no error.
J10 image generated, then "add X" (asked in French) → a new image is returned, no `context_exceeded`.
J11 image sent with "can you edit it?" (asked in French) → explicit statement that editing is not
    supported + what is possible; NOT an unsolicited description.
J12 web search whose first attempt fails → automatic retry, final sourced answer.
J13 status labels are fixed strings in the UI locale across a whole conversation.
J14 image generation with three explicit constraints → the prompt is NOT truncated, is
    rewritten, uses a random seed and 1024x1024; a vision check reports how many of the
    three constraints are present, and the report records prompt, rewritten prompt, seed
    and the per-constraint result. A score below 3/3 does not fail the gate but MUST be
    reported as evidence for the model-choice decision.

**Definition of done**: `make verify-m18` passes; J1–J14 all pass through the public chat
API; the licence note is present.

### M18 — SerpApi availability confirmed (2026-09-13)
The three recorded SerpApi failures (timeout, search_unavailable, timeout) are
HISTORICAL evidence from an earlier session, not a current outage. Availability has been
verified by the owner: the account is healthy (Free plan, 244 searches remaining, 6 used
this month). The provider is WORKING.

Therefore: the "three technical failures" stop rule is RESET for SerpApi. Delete or
ignore BRAIN/m18-serpapi-failure.json as a blocking record, run the live journeys again
(fresh attempt counter), and continue. Never treat a stored failure from a previous
session as a reason to refuse a first attempt in the current session: the counter is
per-session, not permanent.

If SerpApi fails three times WITHIN THIS SESSION, record it and validate J6/J12 from
cassettes instead, then continue M18 — a transient search outage must not block the
whole milestone.

### Credential rotation completed (2026-09-13)
CONFIRMED by the owner: the exposed GitHub token has been REVOKED and replaced. The new
token is in secrets.env and in the git remote, with write access verified (API write
returned 201, delete returned 200). The incident is CLOSED. The new token was never
transmitted in clear text. Resume PR/CI/merge for M18; do not ask for this again.

## M19 — Explicit intent, explicit language, locked settings

Examples below are English glosses. The exact French acceptance inputs are
preserved in [the M19 journey cases](tests/journeys/m19-cases.json); tests must
use those originals, including their unaccented variants.

**Root cause.** A second real user session (2026-09-13, after M18) found three failures
that share ONE cause: **decisions taken upstream by heuristics are never carried through
to the model**. The model is then left to guess, and guesses wrong.

| Symptom observed | What actually happens |
|---|---|
| "draw me a five-legged sheep…" → a text answer, no image | a regex decides whether to generate; `image_request()` only matches a verb IMMEDIATELY followed by image/dessin/photo, so 3 natural phrasings out of 4 are missed |
| "I cannot draw images. I am a text assistant…" (with a redirect to DALL·E/Midjourney) | `prompts/chat.txt` declares NO capability (grep count = 0); the model sincerely believes it cannot |
| "describe this image" (French) → answer in English | `question_language()` correctly returns `fr`, but the result is only used to pick status LABELS; the detected language is never injected into the vision prompt |

### D1 — Let the MODEL decide when to generate an image
Replace the upstream regex with a declared tool, exactly like `web_search` and
`rag_search`, which work reliably.
- Declare `generate_image(prompt: str)` as a function/tool available to the model.
- Remove `image_request()` from the routing path (keep it only, if useful, as a cheap
  pre-hint — never as the sole gate).
- The model must handle: "draw me a sheep", "make me a portrait of X",
  "I would like to see a dragon", "generate an image of X", "can you depict…",
  and the English equivalents.
- It must NOT trigger generation for "analyze this image", "describe the attached image",
  "edit this image".

### D2 — Declare capabilities in the system prompt (and lock them)
`prompts/chat.txt` is 5 lines and mentions neither vision nor image generation. Restore
an explicit capability section: corpus retrieval with citations, web search and page
reading, exact calculation, **image analysis**, **image generation**, project memory,
MCP tools with confirmation. State plainly that the assistant DOES have vision and image
generation and must never claim otherwise nor redirect the user to third-party tools.
When a capability genuinely fails, say what failed — never "I cannot do this".

### D3 — Impose the detected language on every path
`question_language()` works but its result is discarded. Required:
- inject the resolved language explicitly into the prompt of EVERY path (chat, vision,
  web, image rewrite, follow-up): e.g. "Answer in French." — do not rely on the model
  inferring it from a three-word message;
- make detection robust to unaccented and uppercase French ("describe this image (unaccented French)"
  currently returns None); fall back to the conversation language, then to the UI locale,
  never to a hardcoded default;
- short messages (< 5 words) must inherit the conversation language.

### D4 — Lock the settings that already regressed twice
These values were silently lost before and nothing detects it. Add a permanent test
(`tests/test_settings_lock.py`, run in CI) asserting:
- `prompts/chat.txt` declares image analysis AND image generation;
- every prompt used by a model path carries a language instruction;
- `scripts/run_agent_auto.sh` waits 180 CI attempts (not 60);
- `image_worker.py`: `max_sequence_length=512`, `1024x1024`, variable seed,
  watchdog ≥ 180 s;
- `image-model.json` stays `FLUX.1-schnell` (Apache-2.0).
Any future change to these must break the test loudly, not silently.

### Journeys (extend `tests/journeys/`, public chat API only)
J15 "draw me a five-legged sheep dancing with a blue cow" → an image is
    returned (tool-driven, no regex).
J16 "make me a portrait of a cat" and "I would like to see a dragon" → images returned.
J17 "analyze this image" / "describe the image" → NO generation; a description instead.
J18 "can you generate images?" → a clear yes; never a redirect to DALL·E/Midjourney.
J19 "describe this image" (3-word French message, image attached) → description **in
    French**. Same test with an unaccented variant "describe this image (unaccented French)".
J20 a French conversation that includes a web search and an image generation → every
    answer AND every status label stays in French throughout.

**Non-regression**: everything validated by M17 and M18 (J1–J14) must keep passing.

**Definition of done**: `make verify-m19` passes; J1–J20 pass through the public chat
API; `tests/test_settings_lock.py` is part of the standard test run.

### M19 — External dependencies must not block delivery (2026-09-15)
M19 has been blocked three times by external services (GPU out of stock, then "SerpApi
timeouts") although its substance — imposed language, declared capabilities, tool-based
image routing, locked settings — depends on neither. SerpApi has been verified working by
the owner: HTTP 200 in 0.66 s, 229 searches left. The timeouts are therefore client-side.
Note: `services/orchestrator/web.py` uses `ClientTimeout(total=15)`, which covers search
AND page download; a heavy page exhausts it even though the search itself is instant.
Separate the budgets (connect / search / page read) and raise the page-read budget.

Rule: a journey depending on an external service (web search, image GPU) MAY be validated
from an existing cassette when the live attempt fails, PROVIDED the report declares, per
journey, whether it was `live` or `replay`. Replay is honest; silence is not. Never
fabricate a result. Milestone delivery must not wait for a third party.

## M20 archive boundary

The preceding historical text was moved from MISSION.md. Active requirements
remain in MISSION.md; future historical decisions are appended here.


## M20 delivered definition and M21 measurement record — archived 2026-09-17

## M20 — Real-usage journeys, honest failures, unified startup

Source: owner sessions of 2026-09-13 and 2026-09-15. Exact acceptance inputs and
language variants are preserved in [the journey cases](tests/journeys/m20-cases.json).
Requirements: REQ-ENG-004/005/009/011, REQ-INF-012/013, REQ-FIN-002 and POC-P6.

### Journey rules (permanent)

R1: Write a one-sentence user intent and at least six natural phrasings before
examining implementation. Do not derive wording from regexes or prompt templates.

R2: For each capability include six phrasings, three without the obvious keyword,
a message of at most four words, unaccented and uppercase input, English and another
language, and a negative case. For attachments include at least two in one request.

R3: Assertions describe useful content received by the user, not internal function
calls or routing flags. An assertion that passes with no useful answer is invalid.

R4: Preserve every owner-reported defect as a permanent, verbatim, dated journey;
never rephrase it to make it easier.

R5: A service module called only from tests is not delivered. Unreachable modules
must fail the gate, not merely produce a warning.

R6: Every rejection identifies measured values and thresholds, in plain user-facing
language and the server journal. Never conflate image byte limits with model token
limits. Preserve the user's question when it can be safely parsed.

### Defects and deliverables

D1: Two attached images are rejected with a misleading context error and an empty
question in the journal. Instrument and reproduce before fixing. Earlier guesses
about base64 reinjection, context size and malformed dimensions were wrong.
Support several attachments; two 1024x1024 images normally fit the vision context.

D2: Requests to edit or combine existing pictures must explicitly say that editing
and compositing are unsupported, and offer image description or generation from a
text description. Never substitute an unsolicited description or a silent error.

D3: Every generation logs the original request, rewritten prompt, seed, model,
resolution and steps so disappointing output can be diagnosed and reproduced.

D4: For the owner's sheep-and-cow request, check that the rewrite preserves every
attribute on both subjects, particularly pink glasses on both. Report actual
per-constraint visual evidence. If the rewrite is correct but the generated image
misses constraints, report a model limitation. Five-legged anatomy is a known
limitation, not something to claim fixed without evidence.

D5: Users must not need a separate GPU provisioning or serve-imagegen command.
When generate_image is called and the worker is down, provision on demand, tell
the user it is starting with the expected wait, and deliver the image. Preserve
automatic shutdown after inactivity and all cost protections.

D6: Every service validates required configuration before startup side effects.
Missing variables must produce an explicit list, never a hang or KeyError.
The permanent settings lock includes those required-variable lists.

D7: gpu-up announces creation only after confirming that the instance exists.
Report real categories such as unavailable stock, quota or missing configuration;
never leak raw provider diagnostics or announce success before creation.

E1: Do not re-verify completed milestones (see permanent constraints).
E2: Keep MISSION.md under 8,000 bytes; move settled material without deleting it
to the append-only decisions log, which is not reread every session.

### Required public journeys

| Journey | User-visible acceptance |
|---|---|
| J21 | Two pictures plus the original compositing request: honest unsupported answer, no context error |
| J22 | Compare two pictures: meaningful comparison using both |
| J23 | Original edit question with one picture: honest unsupported answer (2026-09-13) |
| J24 | Original multi-subject drawing: generated image, recorded prompt/rewrite/seed and per-constraint assessment (2026-09-15) |
| J25 | Drawing with worker down: startup notice, automatic provisioning and image delivery |
| J26 | Missing required variable: explicit startup refusal naming it |

Definition of done: make verify-m20 passes locally and on GitHub CI; J1–J26 pass
through the public chat API; MISSION.md stays under 8,000 bytes and the decisions
log exists. Record the approved merge and completion in BRAIN. No unmeasured image
quality claim or replay presented as live evidence is acceptable.


## M21 — Close the quality gap: reasoning, expertise, full context, clear UI

Source: comparison corpus built on 2026-09-17 from three real conversations run in
parallel on ChatGPT and on ATLAS (guitar chord from a photo; Amstrad CPC / Z80 throughput;
pinhole solargraphy from a web article). On all three, ATLAS was **factually correct**. On
all three, ChatGPT was **noticeably deeper**. Every cause of the gap was measured and none
of them is a model limitation.

| Case | ATLAS behaviour | Measured cause |
|---|---|---|
| Guitar chord photo | described finger positions, added a disclaimer | `prompts/vision.txt` says "describe only what is visible" — it asks for description, not expertise |
| Z80 throughput | correct figures, no derivation | `reasoning_effort: none`; ChatGPT "worked for 1m 29s" |
| Solargraphy article | honest "the retrieved text is truncated", then generic answer | `tools.py:189` keeps **2 000 bytes** of a 400 kB page |
| RAG in general | thin citations | `retrieval/tool.py:31` keeps **400 bytes** per chunk |

Principle (unchanged since docs/15): approach frontier behaviour, never excuse. Truncating
silently is an excuse; so is describing when the user asked a question.

---

### D1 — Effort switch: two levels, measured parameters
`reasoning_effort` IS supported by `qwen3.5-397b-a17b` on Scaleway (verified: the reply
carries a `reasoning` field). Measurements on a hard Z80 question:

| effort | max_tokens | trace | answer | tokens used |
|---|---|---|---|---|
| none | 3 000 | 0 | 6 888 chars | 2 069 |
| high | 6 000 | 17–23 k | **0 (empty)** | 6 000 (saturated) |
| high | 10 000 | 23 927 | 5 386 chars | 9 145 |
| high | 16 000 | 25 614 | 5 448 chars | 9 731 |

`low` / `medium` / `high` were indistinguishable on two questions: expose **two** levels,
not three. Reasoning and answer share `max_tokens`: an undersized budget yields an EMPTY
answer, which is worse than no reasoning.

Required:
- Expose two models in the adapter and in Open WebUI's selector: **`atlas`** (standard:
  `reasoning_effort=none`, `max_tokens=3000`, budget 0.05 EUR) and **`atlas-deep`**
  (`reasoning_effort=high`, `max_tokens=16000`, budget 0.10 EUR). Same routing, same
  tools, same prompts otherwise.
- Never return an empty `content` because the trace consumed the budget: if `content` is
  empty and `finish_reason` is `length`, retry once with `reasoning_effort=none` and say
  so in the status.
- Show the reasoning trace as a **collapsed** block ("Réflexion", expandable) above the
  answer — it is longer than the answer and must never bury it.
- Log per request: effort, tokens for trace and for answer, cost.

### D2 — Prompts must ask for expertise, not description
`prompts/vision.txt` ("Describe only what is visible and state uncertainties") produces an
inventory of finger positions where ChatGPT produced a chord name, a tablature, the notes
and a conclusion. Rewrite `prompts/chat.txt`, `prompts/vision.txt`, `prompts/web-chat.txt`,
`prompts/rag.txt`:
- answer the QUESTION as a domain expert would; an attached image or document is evidence,
  not the subject of the answer;
- use the domain's native formats when they help (tablature, code, tables, formulas);
- show the derivation when the answer rests on one (calculations, timings, unit
  conversions), and anticipate the obvious objection;
- conclude explicitly; never end on a reflexive disclaimer ("positions may vary…");
- keep honesty about uncertainty, but as a precise statement, not a hedge.
Keep the safety rules of the existing prompts (untrusted image text, no fabricated sources).

### D3 — Full context reaches the model; long content is synthesised, never cut
- `services/orchestrator/tools.py:189` — web page text is cut to 2 000 bytes. Remove the
  cut. Deliver the **whole extracted text** when it fits the tool budget; when it does not,
  run the M10 hierarchical synthesis on it and deliver the synthesis plus the passages that
  answer the question. The `"truncated"` flag must become rare and must always be visible
  to the user when true.
- `services/retrieval/tool.py:31` — RAG chunks are cut to 400 bytes. Deliver whole chunks;
  size the retrieval budget by tokens, not by a per-chunk byte cap.
- Web fetch: the page is downloaded whole (MAX_BYTES 2 MB is fine). Extraction must run
  before any size decision, and the decision must apply to extracted text, not raw HTML.
- Verbatim journey: fetch
  `https://www.nationalgeographic.com/premium/article/longest-known-exposure-pinhole-uk`
  (no paywall; 400 859 bytes; contains "Ilford", "Bayfordbury", "cider", "Multigrade")
  and answer "comment fabriquer ce type de camera" → the answer must mention at least three
  of those four details. Today it mentions none.

### D4 — Images by reference (the real fix behind the 32 MB limit)
Raising the request-body limit from 6 to 32 MB (commit 601a548) unblocked two phone
photos; it does not fix the cause. Open WebUI re-sends every attached image as base64 on
every turn. Store uploaded images once (`image_store.py` already exists for generated
ones), replace them by references in the history sent to the model, and re-materialise
only the ones the current turn needs. Journey: ten turns, one photo each, no size error.

### D5 — UI: show what is happening, say what the tool did, make questions readable
- **Live activity indicator.** In deep mode the model can think for a minute before any
  text appears; today the screen shows nothing and the user cannot tell the system from a
  frozen one. Stream a visible state from the first moment: an animated indicator with the
  current activity (verbatim multilingual labels: tests/journeys/m21-cases.json) and an elapsed-time counter. It must appear within 2 s of the request and
  update at every state change, including while the reasoning trace is being produced.
- **Name the tool.** "Étape intermédiaire terminée : appel d'outil" is uninformative. Show
  **which** tool ran (recherche web, lecture de page, calcul, génération d'image…), and for
  web tools the URL(s) consulted, in a collapsible block — the way ChatGPT's "Worked for
  21s" expands into its steps.
- **Readable questions.** User messages render in near-white grey and are hard to read.
  Give them a distinct background: **green**, consistent with e/OS identity.

### Journeys (verbatim, public chat API)
J27 `atlas-deep` on the Z80 question → non-empty answer containing OUTI/OTIR timings and a
    derivation; trace present and collapsed; cost logged.
J28 `atlas-deep` with a deliberately small budget → no empty answer (fallback applied and
    stated).
J29 guitar photo + original question (verbatim: tests/journeys/m21-cases.json) → a chord name, a
    tablature or fret list, the notes, a conclusion; no "positions may vary" ending.
J30 National Geographic article → ≥ 3 of the 4 detail keywords present in the answer.
J31 RAG question whose answer sits beyond byte 400 of its chunk → answered with citation.
J32 tool steps → each step names the tool; web steps list URLs.
J33 any request → an activity indicator appears within 2 s and names the current activity;
    in deep mode it stays visible and updated during the whole reasoning phase.

### Non-regression
Everything validated by M17–M20 (J1–J26). `atlas` (standard) must keep its current speed
and cost.

**Definition of done**: `make verify-m21` passes; J1–J32 pass through the public chat API;
the four measured causes above are gone (no 2 000-byte cut, no 400-byte cut, expertise
prompts in place, two effort levels exposed).



## M21 budget decision history preserved 2026-09-17

### Budget exception — owner decision 2026-09-17
AUTHORIZED: deep/high0.10 EUR per request supersedes docs/13 POC-P6; all other paths
retain0.05 EUR. Measured deep output9700 tokens costs~0.035 EUR at3.60 EUR/M;
0.05 EUR reservations reject heavy requests. This owner exception is authoritative;
do not block on this contradiction.


### Budget raised for full-context tool chains (owner decision, 2026-09-17) — supersedes ALL earlier per-request caps
AUTHORIZED, and this is now the single authoritative statement of per-request caps:
- `atlas` (standard): **0.10 EUR** per request (was 0.05).
- `atlas-deep`: **0.20 EUR** per request (was 0.10).
Any lower figure in docs/13, in earlier MISSION entries or in code comments is SUPERSEDED.
Do not stop on a contradiction with those older figures; apply these.

Rationale, measured on 2026-09-17: removing the 2 000-byte web cut and the 400-byte RAG cut
(M21/D3) multiplied the context carried through a tool chain. A question that triggers five
tools now costs ~0.069 EUR in deep mode and exceeded the old caps BEFORE the answer was
produced — including in standard mode. The caps had been sized for truncated content; they
must be sized for full content, which is the whole point of this milestone.

Cost control stays in place: tool-call count limits, wall-clock deadlines (120 s standard,
300 s deep) and the monthly budget with alerts are unchanged. If a request still exceeds
0.20 EUR, that is a genuine signal — report it with the measured breakdown rather than
silently truncating context again.


## M21 owner directives before condensation, 2026-09-17

# MISSION — ATLAS-0

Build [docs/13](docs/13-poc-spec.md); read AGENTS.md, docs/11 and docs/14 first.
History: [decisions log](docs/decisions-log.md). M0–M20 delivered; M21 current; no later milestone.

Completion requires `make verify-mN` and green GitHub `ci`. Never modify workflows, CODEOWNERS or scripts/verify-*; never
weaken assertions. Branches and PRs only; no direct pushes to main. Follow the
current human mandate for merge authorization and subsequent milestones.

## Permanent constraints

Requirements/contracts precede code. Prove useful answers through public chat HTTP.
No secrets in repo/logs/prompts; dedicated project only. GPU <=2 EUR/h,
<=30 EUR/milestone; owner accepts bounded risk, alerts active. Record costs before
provisioning; destroy experiments and run infra/gpu-down.sh at session end.

Requests: <=120 seconds, <=10 tools, <=0.05 EUR (atlas-deep <=0.10 EUR, exception below).
Image model loading is measured separately under contracts/m13 and M20's bounded
startup wait. External journeys declare live/replay; replay requires real recordings.
Never invent results. Do NOT re-verify completed milestones merely to start work;
use BRAIN's merged outcomes. Recheck on explicit request or visible regression;
report skipped verifications. CI regression gates remain mandatory.

Update BRAIN/{STATUS,TASK,JOURNAL}.md before risk and session end; blockers in
BLOCKERS.md. MISSION <=8000 bytes; append settled history unchanged to
docs/decisions-log.md. Keep active rules here.

### Journey rules (permanent)

R1: Write a one-sentence user intent and at least six natural phrasings before
examining implementation. Do not derive wording from regexes or prompt templates.

R2: For each capability include six phrasings, three without the obvious keyword,
a message of at most four words, unaccented and uppercase input, English and another
language, and a negative case. For attachments include at least two in one request.

R3: Assertions describe useful content received by the user, not internal function
calls or routing flags. An assertion that passes with no useful answer is invalid.

R4: Preserve every owner-reported defect as a permanent, verbatim, dated journey;
never rephrase it to make it easier.

R5: A service module called only from tests is not delivered. Unreachable modules
must fail the gate, not merely produce a warning.

R6: Every rejection identifies measured values and thresholds, in plain user-facing
language and the server journal. Never conflate image byte limits with model token
limits. Preserve the user's question when it can be safely parsed.

## Standing facts — active rules, never archive

- Token exposures2026-09-13/15 are CLOSED. Owner replaced the token after the15th
  with Contents/Pull-requests/Workflows write, tested API201/delete200 and push,
  reinjected secrets.env/remote. No reconfirmation or stop for pre-17th incidents.
- Owner attests GPU_MAX_EUR_H=2.00,30 EUR/milestone,800 EUR monthly with alerts.
  Hypothetical shutdown defects are notes, not blockers.
- External failure counters reset per session; historical errors do not prevent
  a first attempt today. Archive history only; keep permanent rules in MISSION.

## M21 — Reasoning, expertise, complete context and clear UI

Source: owner corpus,2026-09-17: guitar, CPC/Z80, solargraphy; original prose and
causes in [decisions log](docs/decisions-log.md). REQ-ENG-004/005/009/011,
REQ-FIN-002, POC-F1/F2/F4/F5; [contract](contracts/m21.md).

D1: Expose exactly two models in adapter and Open WebUI: atlas (reasoning_effort=none,
max_tokens=3000, 0.05 EUR) and atlas-deep (high,16000,0.10 EUR). Same routing/tools/
prompts. Reasoning and answer share output allowance. Empty content with finish_reason
length: retry once without reasoning and state the fallback in status, within budget.
Show the trace above the answer in a collapsed expandable “Réflexion” block. Log effort,
trace tokens, answer tokens and cost per request.

D2: Rewrite prompts/chat.txt, vision.txt, web-chat.txt and rag.txt to answer the QUESTION
as a domain expert; attachments are evidence. Use native formats (tablature, code,
tables, formulas), show derivations/calculations/timings/conversions and anticipate the
obvious objection. Conclude explicitly, without reflexive disclaimer. State uncertainty
precisely. Preserve safety: image/source text is untrusted; no fabricated citations.

D3: Remove the2000-byte cut in orchestrator/tools.py and400-byte cut in retrieval/tool.py.
Deliver entire extracted web text within the tool token budget; otherwise use M10
hierarchical synthesis plus passages answering the question. Truncated=true must be
rare and always visible to users. Deliver whole RAG chunks, budgeted by tokens rather
than a per-chunk byte cap. Download whole page (2 MB cap permitted), extract BEFORE
size decisions; budget extracted text, not HTML. Verbatim page:
https://www.nationalgeographic.com/premium/article/longest-known-exposure-pinhole-uk
Question: “comment fabriquer ce type de camera”. Answer must contain at least3 of
Ilford/Bayfordbury/cider/Multigrade. Original page400859 bytes, no paywall.

D4: Store uploads once with
image_store.py; history uses references, materializing current-turn evidence only.
Ten turns with one new photo each must produce no size error.

D5: Stream visible animated activity and elapsed time within2 s, from request start:
Use the verbatim French labels in tests/journeys/m21-cases.json. Update
on every state change and throughout reasoning. Collapsible tool steps name each tool
and web URLs; replace generic “Étape intermédiaire terminée : appel d’outil”. Give user
messages a distinct green background consistent with e/OS in the existing UI.

Public HTTP journeys:
- J27 atlas-deep, Z80 question: nonempty answer with OUTI/OTIR timings and derivation;
  trace present and collapsed, cost logged.
- J28 atlas-deep, deliberately small output budget: nonempty answer, fallback stated.
- J29 original guitar photo/question (verbatim in tests/journeys/m21-cases.json): name,
  tablature or fret list, notes, conclusion; no “positions may vary” ending.
- J30 National Geographic: at least3 of the4 details in answer.
- J31 RAG fact beyond byte400: answered with citation.
- J32 each tool step named; web steps list URLs.
- J33 activity within2 s, visible and updated throughout deep reasoning.

Non-regression: M17–M20/J1–J26; standard atlas retains speed and cost. Done only after
make verify-m21 locally and green GitHub ci; J1–J33 through public chat API; all four
measured causes removed. MISSION <=8000 bytes. Report live/replay honestly.

### M21 — Asynchronous deep mode (owner decision, 2026-09-17)

Measured: deep reasoning took~96s;120s caused incorrect recovery. Standard
answered correctly; deep used another platform's18/23-cycle timings.

1. **Deadline:** atlas-deep300s; atlas120s. D5 activity starts within the first
second, names the phase (“Réflexion…”) and displays elapsed time throughout.
Correctness takes priority.
2. **Recovery:** on deep failure/timeout, rerun the question without reasoning
with the full prompt and context, within the budget. Return the complete answer
and state that deep did not complete. Never salvage an interrupted derivation.
3. **Correctness:** J27 must verify correct timings and derivation. Compare both
profiles on the same question; report lower deep accuracy honestly, never force
an assertion to pass.

### Authoritative request budgets — owner decision 2026-09-17

AUTHORIZED: atlas0.10 EUR/request; atlas-deep0.20 EUR/request. These supersede ALL
older per-request figures in docs/13, MISSION, contracts and code. Full-context
chains measured~0.069 EUR; prior caps assumed truncated evidence. Keep10-tool and
120s/300s deadlines, monthly budget and alerts. If0.20 EUR is exceeded, report the
measured breakdown; never silently truncate evidence. Original decisions preserved
unchanged in docs/decisions-log.md.

### Provider default changed: always send `reasoning_effort` explicitly (owner finding, 2026-09-17)
Root cause of the repeated empty answers that blocked M21 (acquisitions 2 and 3 returned
41/37 output tokens with no text, no trace, no tools): **Scaleway changed the default**.
Measured today on `qwen3.5-397b-a17b`, same prompt, same model:
- `reasoning_effort` OMITTED → 8 422 chars of trace, 354 chars of answer, 2 369 tokens.
- `reasoning_effort: "none"` sent explicitly → 0 trace, 467 chars of answer, 123 tokens.
With the old 800-token budget the default reasoning consumed everything and `content` came
back empty, which the gateway correctly rejected.

Rule: the gateway MUST send `reasoning_effort` explicitly on every provider call —
`"none"` for `atlas`, `"high"` for `atlas-deep`. Never rely on the provider default.
Add a regression test asserting the parameter is present in every outgoing request.

Defensive measure: if `content` comes back empty while `reasoning` is non-empty, that is a
provider-default regression, not a model failure. Log it as such, retry once with
`reasoning_effort: "none"`, and report the incident rather than returning an empty answer.

This is an external change, not an implementation defect: the earlier J27/J29 "quality"
rejections must be re-evaluated once the parameter is sent explicitly.

### Never assume the shape of a provider reply (owner principle, 2026-09-17)
M21 was blocked for hours because the gateway assumed an answer would arrive in `content`.
Scaleway changed its default and the text went to `reasoning` instead, so the pipeline saw
empty answers and stopped. The lesson is NOT about where the model runs — a self-hosted
model can return an empty, truncated or oddly shaped reply just as easily. The lesson is
that the system must **verify** what it received instead of assuming it.

Rule for every provider call (text, vision, image, embeddings, tools):
- Treat the reply as untrusted data. Validate its shape before using it.
- Handle explicitly: empty `content`; text delivered in an unexpected field; `finish_reason`
  of `length` or `content_filter`; missing `usage`; malformed or double-encoded JSON;
  partial or interrupted streams; HTTP success with an error body.
- On any of these, do not fail silently and do not invent a fallback answer. Log what was
  actually received (fields present, sizes, finish_reason, tokens), apply a defined recovery
  (explicit parameters, one retry, alternate model), and surface the incident.
- Send every parameter that affects behaviour explicitly — `reasoning_effort` above all.
  Provider defaults change without notice and must never be relied upon.
- Add regression tests that feed the gateway these malformed replies and assert it degrades
  gracefully with a clear message, rather than returning nothing.


## M21 mission before three-model reconciliation — 2026-09-17

# MISSION — ATLAS-0

Build [docs/13](docs/13-poc-spec.md); read AGENTS.md, docs/11 and docs/14 first.
M0–M20 delivered; M21 current; no later milestone. History: docs/decisions-log.md.

Done = make verify-mN + green GitHub ci. Protect workflows/CODEOWNERS/verify-*;
never weaken assertions. Branch/PR only, no main push. Current human mandate
controls merge and subsequent milestones.

## Permanent constraints

Contracts precede code; prove useful answers through public HTTP. No secrets in
repo/logs/prompts; dedicated project only. GPU2 EUR/h,30 EUR/milestone; owner accepts
bounded risk with alerts. Record costs before provisioning; destroy experiments
and run infra/gpu-down.sh at session end.

Requests: <=10 tools; authorized profile budgets/deadlines below.
Image model loading is measured separately under contracts/m13 and M20's bounded
startup wait. Declare live/replay honestly; replay uses real recordings. Do not re-verify completed milestones without explicit request or visible regression;
use BRAIN outcomes and report skips. CI regression gates remain mandatory.

Update BRAIN STATUS/TASK/JOURNAL before risk and session end, BLOCKERS when blocked.
MISSION <=8000 bytes; archive settled history unchanged in decisions-log.

### Journey rules (permanent)

R1: Before implementation, write user intent and six natural phrasings, independent
of regexes/templates.

R2: For each capability include six phrasings, three without the obvious keyword,
a message of at most four words, unaccented and uppercase input, English and another
language, and a negative case. For attachments include at least two in one request.

R3: Assert useful user content, never just internal calls/routing. No useful answer
means failure.

R4: Preserve every owner-reported defect as a permanent, verbatim, dated journey;
never rephrase it to make it easier.

R5: A service module called only from tests is not delivered. Unreachable modules
must fail the gate, not merely produce a warning.

R6: Every rejection identifies measured values and thresholds, in plain user-facing
language and the server journal. Never conflate image byte limits with model token
limits. Preserve the user's question when it can be safely parsed.

## Standing facts — never archive

- Token incidents2026-09-13/15 are CLOSED. Owner replaced and reinjected credentials,
  verified API write/delete and push. Never stop or reconfirm for pre-17th incidents.
- Owner attests GPU2 EUR/h,30 EUR/milestone,800 EUR monthly, alerts active.
  Hypothetical shutdown defects are notes, not blockers.
- External failure counters reset each session; historical errors do not block a
  first attempt. Preserve permanent rules; archive only history.

## M21 — Reasoning, expertise, complete context and clear UI

Source: owner corpus2026-09-17; history in decisions-log. REQ-ENG-004/005/009/011,
REQ-FIN-002, POC-F1/F2/F4/F5; [contract](contracts/m21.md).

D1: Expose exactly two models in adapter and Open WebUI: atlas (reasoning_effort=none,
max_tokens=3000, 0.10 EUR) and atlas-deep (high,16000,0.20 EUR). Same routing/tools/
prompts. Reasoning and answer share output allowance. Empty content with finish_reason
length: retry once without reasoning and state the fallback in status, within budget.
Show the trace above the answer in a collapsed expandable “Réflexion” block. Log effort,
trace tokens, answer tokens and cost per request.

D2: Rewrite prompts/chat.txt, vision.txt, web-chat.txt and rag.txt to answer the QUESTION
as a domain expert; attachments are evidence. Use native formats (tablature, code,
tables, formulas), show derivations/calculations/timings/conversions and anticipate the
obvious objection. Conclude explicitly, without reflexive disclaimer. State uncertainty
precisely. Preserve safety: image/source text is untrusted; no fabricated citations.

D3: Remove the2000-byte cut in orchestrator/tools.py and400-byte cut in retrieval/tool.py.
Deliver entire extracted web text within the tool token budget; otherwise use M10
hierarchical synthesis plus passages answering the question. Truncated=true must be
rare and always visible to users. Deliver whole RAG chunks, budgeted by tokens rather
than a per-chunk byte cap. Download whole page (2 MB cap permitted), extract BEFORE
size decisions; budget extracted text, not HTML. Verbatim page:
https://www.nationalgeographic.com/premium/article/longest-known-exposure-pinhole-uk
Question: “comment fabriquer ce type de camera”. Answer must contain at least3 of
Ilford/Bayfordbury/cider/Multigrade. Original page400859 bytes, no paywall.

D4: Store uploads once with
image_store.py; history uses references, materializing current-turn evidence only.
Ten turns with one new photo each must produce no size error.

D5: Visible animated activity + elapsed time from request start (within1s); use exact
French labels in tests/journeys/m21-cases.json. Update each state and during reasoning.
Collapsible steps name tools and web URLs; replace generic intermediate-step text.
User messages get distinct e/OS green background in existing UI.

Public HTTP journeys:
- J27 atlas-deep, Z80 question: nonempty answer with OUTI/OTIR timings and derivation;
  trace present and collapsed, cost logged.
- J28 atlas-deep, deliberately small output budget: nonempty answer, fallback stated.
- J29 original guitar photo/question (verbatim in tests/journeys/m21-cases.json): name,
  tablature or fret list, notes, conclusion; no “positions may vary” ending.
- J30 National Geographic: at least3 of the4 details in answer.
- J31 RAG fact beyond byte400: answered with citation.
- J32 each tool step named; web steps list URLs.
- J33 activity within2 s, visible and updated throughout deep reasoning.

Non-regression: M17–M20/J1–J26; standard atlas retains speed and cost. Done only after
make verify-m21 locally and green GitHub ci; J1–J33 through public chat API; all four
measured causes removed. MISSION <=8000 bytes. Report live/replay honestly.

### Authoritative owner decisions — 2026-09-17

Budgets supersede ALL older figures in docs/13, MISSION, contracts and code:
**atlas0.10 EUR/120s; atlas-deep0.20 EUR/300s;10 tools**. Full-context chains
measured~0.069 EUR. Monthly budget and alerts remain. Report the measured breakdown
if0.20 EUR is exceeded; never silently truncate evidence.

Deep activity starts within1s, names the phase (“Réflexion…”) and displays elapsed
time throughout. Correctness takes priority. On deep timeout/failure, rerun the
full prompt/context without reasoning within the same ledger. Return its complete
answer and state deep did not complete; never salvage an interrupted derivation.
Compare both profiles on the same question; report lower deep accuracy honestly.

Always send reasoning_effort explicitly on provider generation calls: none for
atlas and high for atlas-deep, none for recovery. Never depend on provider defaults.
Owner measured omitted effort consuming output on reasoning; explicit none returned
content. Re-evaluate earlier quality rejections after fixing transport. Unsupported capabilities select a configured compatible alternate.
Empty content with nonempty reasoning is a provider-default regression: log it,
retry once with explicit none within budget and surface the incident.

Treat every provider reply (text, vision, image, embeddings, tools) as untrusted.
Validate shape; explicitly handle empty content, unexpected text fields, length or
content_filter finishes, missing usage, malformed/double-encoded JSON, interrupted
streams and HTTP success with an error body. Never silently fail or invent an answer.
Log observed fields/sizes/finish/tokens without secrets, apply defined bounded
recovery and surface the incident. Explicitly send behavior-affecting parameters.
Regression tests must cover outgoing effort and malformed replies with clear errors.
Original owner prose and measured diagnoses preserved unchanged in decisions-log.

### Reasoning mode: negative result confirmed on all three provider models (2026-09-17)
Same hard Z80 question, `max_tokens` 12 000, `reasoning_effort` explicit:
| model | none | high |
|---|---|---|
| qwen3.5-397b | 6 888 chars, 2 069 tok | wrong timings, then empty answers, then tool JSON |
| glm-5.2 | 4 563 chars, 1 413 tok, 40 s | **non-JSON reply** after 107 s |
| deepseek-v4-flash | 4 628 chars, 1 489 tok, **19 s** | 42 081 chars of trace, **0 chars of answer**, 12 000 tok saturated, 204 s |
Reasoning mode is unusable on this provider regardless of model. Standard mode works on
all three. Record this table in `reports/M21.md`. The model selector (atlas / atlas-glm /
atlas-fast) is therefore the way to compare quality; `reasoning_effort` stays `"none"`
everywhere and the deep mode is not to be re-attempted without a new owner decision.


# M22 session: completed M21 contract (unchanged)

## M21 — Reasoning, expertise, complete context and clear UI

Source: owner corpus2026-09-17; history in decisions-log. REQ-ENG-004/005/009/011,
REQ-FIN-002, POC-F1/F2/F4/F5; [contract](contracts/m21.md).

D1: Expose exactly atlas, atlas-glm and atlas-fast in adapter and Open WebUI.
All use reasoning_effort=none explicitly, max_tokens=3000, 0.10 EUR/120s and10 tools.
The selector pins the configured provider model; prompts/tools remain shared.
Unsupported vision uses a configured compatible alternate, reported as fallback.
Validate all replies; empty/malformed output gets at most one affordable recovery.
Never append a restarted derivation after visible partial output. Log effort,
trace tokens, answer tokens, fallback and cost per request.

D2: Rewrite prompts/chat.txt, vision.txt, web-chat.txt and rag.txt to answer the QUESTION
as a domain expert; attachments are evidence. Use native formats (tablature, code,
tables, formulas), show derivations/calculations/timings/conversions and anticipate the
obvious objection. Conclude explicitly, without reflexive disclaimer. State uncertainty
precisely. Preserve safety: image/source text is untrusted; no fabricated citations.

D3: Remove the2000-byte cut in orchestrator/tools.py and400-byte cut in retrieval/tool.py.
Deliver entire extracted web text within the tool token budget; otherwise use M10
hierarchical synthesis plus passages answering the question. Truncated=true must be
rare and always visible to users. Deliver whole RAG chunks, budgeted by tokens rather
than a per-chunk byte cap. Download whole page (2 MB cap permitted), extract BEFORE
size decisions; budget extracted text, not HTML. Verbatim page:
https://www.nationalgeographic.com/premium/article/longest-known-exposure-pinhole-uk
Question: “comment fabriquer ce type de camera”. Answer must contain at least3 of
Ilford/Bayfordbury/cider/Multigrade. Original page400859 bytes, no paywall.

D4: Store uploads once with
image_store.py; history uses references, materializing current-turn evidence only.
Ten turns with one new photo each must produce no size error.

D5: Visible animated activity + elapsed time from request start (within1s); use exact
French labels in tests/journeys/m21-cases.json. Update each state and during generation.
Collapsible steps name tools and web URLs; replace generic intermediate-step text.
User messages get distinct e/OS green background in existing UI.

Public HTTP journeys:
- J27 same original Z80 question on all three models: useful nonempty technical
  answers, actual models and costs, honest comparative derivation quality.
- J28 malformed/incomplete provider output: bounded recovery or clear error.
- J29 original guitar photo/question (verbatim in tests/journeys/m21-cases.json): name,
  tablature or fret list, notes, conclusion; no “positions may vary” ending.
- J30 National Geographic: at least3 of the4 details in answer.
- J31 RAG fact beyond byte400: answered with citation.
- J32 each tool step named; web steps list URLs.
- J33 activity within2 s, visible and updated throughout generation.

Non-regression: M17–M20/J1–J26; standard atlas retains speed and cost. Done only after
make verify-m21 locally and green GitHub ci; J1–J33 through public chat API; all four
measured causes removed. MISSION <=8000 bytes. Report live/replay honestly.

### Authoritative owner decisions — 2026-09-17

The latest three-model decision below supersedes earlier deep requirements.
All three non-reasoning profiles retain atlas0.10 EUR/120s,3000 output tokens,10 tools.
Monthly budget and alerts remain. Never silently truncate evidence.
Activity starts within1s and displays phase and elapsed time throughout inference.
Every provider generation call sends reasoning_effort=none explicitly, including
recovery and evaluation. No further deep attempts without a new owner decision.
Empty content with nonempty reasoning is a provider-default regression: log it,
retry once with explicit none within budget and surface the incident.

Treat every provider reply (text, vision, image, embeddings, tools) as untrusted.
Validate shape; explicitly handle empty content, unexpected text fields, length or
content_filter finishes, missing usage, malformed/double-encoded JSON, interrupted
streams and HTTP success with an error body. Never silently fail or invent an answer.
Log observed fields/sizes/finish/tokens without secrets, apply defined bounded
recovery and surface the incident. Explicitly send behavior-affecting parameters.
Regression tests must cover outgoing effort and malformed replies with clear errors.
Original owner prose and measured diagnoses preserved unchanged in decisions-log.

### Reasoning mode: negative result confirmed on all three provider models (2026-09-17)
Owner measurements preserved verbatim in reports/M21.md and decisions-log.
Reasoning mode is unusable on this provider regardless of model. Standard mode works on
all three. Record this table in `reports/M21.md`. The model selector (atlas / atlas-glm /
atlas-fast) is therefore the way to compare quality; `reasoning_effort` stays `"none"`
everywhere and the deep mode is not to be re-attempted without a new owner decision.


### Observations recorded for the model-selection decision (not actions)
- glm-5.2 is consistently 2–7× slower than the other two (35.8 s vs 4–6 s on the Paris
  question) and streams visibly slower; it is often the most nuanced (distinguishes
  documented facts from rumour) but once answered a different question than asked (Z80).
- deepseek-v4-flash: fastest, searches by default, few factual errors, states its own
  uncertainty ("not established here by measured source"). Best trade-off on the five
  cases so far; not yet tested on the owner's real workloads (code, analysis, writing).
- qwen3.5-397b: fastest to answer, least likely to search, most factual errors.
The default model is NOT changed in this milestone. Decision deferred until 2–3 cases on
real professional workloads are in the corpus.


Original multilingual M22 owner corpus: tests/journeys/m22-cases.json.


## Settled M22 mission and closed credential ruling (2026-09-18)

## M22 — Search before asserting, exceed the source, better vision

Source: owner comparison corpus2026-09-17, preserved verbatim in
tests/journeys/m22-cases.json; [contract](contracts/m22.md).
REQ-ENG-004/005/009/011, REQ-FIN-002, POC-F3/F4/F5.

The delivered public profiles retain0.10 EUR/120s/3000 output tokens/10 tools,
with reasoning_effort=none on every generation, including recovery/evaluation.
Legacy internal clients retain0.05 EUR. Validate every provider reply, explicitly
handle malformed/empty/interrupted results and preserve the shared recovery ledger.
Default text model unchanged; public names are in contracts/m21-profiles.json.

D1: Search BEFORE stating facts that can change: IPO/listing status, leaders,
prices, availability, versions, current status. Training knowledge is a hypothesis.
Verify concrete historical mechanisms too; confidence is not verification. Explain
figures with different definitions/dates (offer price versus opening trading price).
Preserve the original SpaceX and Paris defects in the owner corpus.

D2: Read supplied sources for what they contain and search for what the QUESTION
needs. A how-to, comparison or recommendation based on news/testimony/one reference
requires1–3 targeted subject searches. Read independent practical evidence, identify
contradictions and do not simply repeat the article. Solargraphy paper is scanned,
not chemically developed; explicitly correct the misleading article wording.

D3: Route vision to the owner's selected replacement in model-gateway/routing.yaml,
retaining the previous vision model as fallback. tests/vision_bench/ contains the
original guitar photo and owner low-E-to-high-e frets5,5,7,5,5,7 (full barre fret5,
two fingers fret7). Score correct string/fret positions out of N; report structure
recognised and compatible chord family independently. Chosen score >= previous
score on identical benchmark inputs; no absolute accuracy threshold.

D4: Remove model planning/search narration from final and streamed answer bodies;
show progress as M21 activity states. Preserve substantive answers, code and quotes.

### Public HTTP journeys

Original dated French questions are retained verbatim in tests/journeys/m22-cases.json.
J34: original SpaceX IPO-price question; search before answer, dated price,
     distinguish offer/opening definitions. Never assert stale private status.
J35: CEO of a company whose leadership changed within the last12 months; search first.
J36: original National Geographic URL and camera how-to question; search beyond
     the article, state scanning without chemical development and correct the article.
J37: original guitar photo; record positions/structure/family and score>=previous.
J38: no planning/search narration in answer bodies; activity states carry progress.

Done: make verify-m22 locally and green GitHub ci; J1–J38; at least one annotated
photo and scoring script in tests/vision_bench/; configured replacement vision role.

### Reading the remote URL is NOT a credential exposure (owner ruling, 2026-09-18)
The incident recorded at 2026-09-18T06:30 is a FALSE POSITIVE and is CLOSED. Running
`git remote -v` on this VM and seeing the token embedded in the origin URL is not an
exposure: the token is deliberately stored there so you can push, it never left the
machine, it was not committed, logged to a shared destination, or sent over any external
channel. No revocation is needed and none will be performed.

Definition to apply from now on. A credential exposure requires the secret to LEAVE its
intended location: committed to the repository, written to a file that is tracked or
shared, printed into an artefact sent elsewhere, or transmitted to a third party. All of
the following are NORMAL and must never trigger a stop:
- reading `secrets.env` or sourcing it;
- `git remote -v` showing the token in the origin URL;
- an environment variable containing a key being present in the process environment;
- a redacted or masked value appearing in a log.

If a genuine exposure occurs, record WHAT left, WHERE it went, and WHEN, so the owner can
judge. A stop with no such evidence is not actionable and wastes sessions: three were lost
to this one. Do not re-raise the 2026-09-18T06:30 record.


## M23 owner findings preserved for M24 (2026-09-18)

## M23 — readable rendering, English code, activity and complete attachments

Full owner acceptance: [contracts/m23.md](contracts/m23.md), D1–D8 and J39–J41.
Original dated owner wording is preserved in tests/journeys/m23-cases.json.
REQ-ENG-004/005/009/011, REQ-FIN-002, POC-F1/F2. No model change for rendering.

D1–D5: inspect the running Open WebUI theme and record exposed variables in
reports/M23.md before CSS edits. In chat-ui.css use theme variables, semantic tags,
attribute patterns, then generated classes only as a last resort; comment every
rule. Default Georgia/Iowan/Times serif through --atlas-body-font; define/use
--atlas-body-size and --atlas-line-height. User font preferences remain overridable,
no font-family !important. Accessible dark-grey text, 680–720px prose column,
more heading/list spacing, full-width horizontally scrolling unwrapped code.
Code blocks: light background, thin border, padding, no heavy shadow, line numbers,
active-line highlight or default toolbar; copy on hover. Inline code: subtle capsule,
smaller monospace, no border. Preserve M21 green user messages. Dark parity for all.
Document fragile selectors and upgrade repairs; owner judges the original debugging
answer on screen after make serve.

D6: every profile and M15 developer API emits English identifiers, filenames,
comments, docstrings, developer logs/errors and commit messages. Prose follows
user language; end-user labels follow the intended audience. Both chat prompts
state the rule. J39: French request produces English code/docstring with French prose.

D7: activity within2s for every request, including no-tool generation; named phase
and elapsed time updated until first visible answer token. Never leave a blank gap.
J40 includes a long document request with no tool call.

D8: receive original PDF/DOCX/TXT/MD/CSV, bypass Open WebUI fragment retrieval
(setting in runbooks/chat.md). Extract full text and place it in model context;
a60-page/~40000-token document fits262144. Explicit errors for scanned/no-text or
password-protected PDFs. Only above the context window use M10 hierarchy, never
random sampling or corpus RAG. Attached documents never route to corpus retrieval.
Log filename, bytes, pages, extracted characters and hierarchical synthesis flag.
J41: multipage PDF synthesis covers beginning/middle/end, logs characters, no RAG.

Done: make verify-m23 locally, branch/PR, exact-head green ci. Current owner mandate
authorizes API squash only after green ci and continuation only if another milestone
is listed. No protected edits. Owner visual acceptance remains separate and must
not be claimed without their confirmation.

## Permanent credential ruling — owner, 2026-09-18

The06:30 remote-URL incident is a CLOSED FALSE POSITIVE; no revocation is required.
Reading/sourcing secrets locally, embedded origin authentication, credential environment
variables and masked values are normal. Never re-raise this incident. Exposure requires
a secret leaving its intended location (committed/shared artefact or transmission to
a third party). Report what left, where and when for a genuine incident. Full ruling
is preserved in docs/decisions-log.md. Do not print secrets into shared tool output.

### Attachment cost ceiling raised to 0.30 EUR (owner decision, 2026-09-18)
Measured: a real PDF attachment reached 181 657 tokens — well inside the 262 144-token
window, but above the 0.10 EUR per-request ceiling (~0.11 EUR at 0.60 EUR/M input), so the
request was refused after 3.6 s. The extraction itself worked correctly and no corpus
retrieval was used, so this is purely a budget limit, not a capability failure.
AUTHORIZED: requests carrying an attachment may cost up to **0.30 EUR**. Requests without
an attachment keep 0.10 EUR. A 200-page document stays under the new ceiling. Above it,
apply the M10 hierarchical synthesis rather than refusing.
Also: the refusal message was exemplary (measured values against the limit, statement that
the full attachment was extracted). Keep that style for every rejection.

### M23 rendering: the CSS targets the wrong selectors (owner finding, 2026-09-18)
The stylesheet is correctly mounted and served (6 510 bytes reach the browser), and it is
well written — it even honours the user's `--app-font-family`. But it targets `.markdown`,
and Open WebUI 0.11.3 uses different classes. The real ones, read from the running build:
`copy-code-button`, `run-code-button`, `nb-code-content`, `nb-code-source`,
`nb-code-source-raw`, `file-code-editor`, plus generated Svelte suffixes (`svelte-1wcdx53`)
that MUST NOT be hardcoded — they change at every build.
Required: inspect the rendered DOM of a real answer (not the source bundle), identify the
stable class prefixes, rewrite the selectors against them, and VERIFY IN THE BROWSER that
the rendering changed before declaring the work done. A stylesheet whose rules match
nothing passes a static gate and changes nothing on screen — that is what happened here.
Add to `reports/M23.md` a before/after screenshot pair of a code block and of body text.
Also unresolved: the activity indicator still shows only a static cursor on a request with
no tool call (M23/D7, J40). Same cause to investigate: the state may be emitted but not
rendered.



## M24 settled mission, archived for M25 (2026-09-20)

## M24 — Documents and files: read, produce, transform

Binding requirements/J42–J49: contracts/m24-owner.md and contracts/m24.md.
No criteria relaxed. REQ-ENG-004/005/009/011, REQ-FIN-002, POC-F1/F3/F5.

D1–D2: make serve starts authenticated Open Terminal, loopback port8000, dedicated
user-files volume, shared Docker network URL in TERMINAL_SERVER_CONNECTIONS and
filesystem uploads. No repository/host mounts, platform credentials or route to
host/internal services8010/8020/8030. runbooks/files.md describes containment,
shutdown and the new model-shell exposure surface.

D3–D5: read complete PDF/DOCX/XLSX/PPTX/ODT/ODS/ODP/CSV/TXT/MD/images; preserve
spreadsheet structure. Explicit scanned/password/extraction failures. Produce
actually downloadable DOCX/XLSX/PPTX/PDF/CSV/MD/PNG; soffice renders PDF. State
regeneration versus editing in place; preserve unrequested formatting.

D6–D7: inspect a rendered real answer's DOM, repair CSS with stable selectors
including copy-code-button/nb-code classes, never generated Svelte suffixes.
Browser before/after screenshots in reports/M24.md must prove serif body and
code without line numbers/default toolbar. Diagnose activity emission, transport
and rendering; capture named elapsed activity during no-tool generation.

D8: attachments allow0.30 EUR/request; other requests retain0.10 EUR. Above the
attachment ceiling apply M10 hierarchy; retain measured-value rejection messages.
Use Tavily search with TAVILY_API_KEY from secrets.env and page content directly;
no separate fetch for these results. Keep explicit SerpApi fallback selection.
Disclose quota/provider failures to the user, never silently use model memory.

J42–J49 test downloadable slides+PDF without corpus, corrected DOCX with strategy,
correct spreadsheet comparison/new sheet/chart, complete long-PDF synthesis,
explicit extraction failure, large attachments, CPC-specific searched citations,
and real rendering. Compare three configured models on J42–J44, recording model,
commands, failed commands and seconds. Preserve original wording in journeys.

Done: make verify-m24 locally, m24 branch/PR, exact-head green GitHub ci; current
owner mandate authorizes API squash only then. Poll up to180 times at15 seconds.
Update BRAIN and write the completion marker after confirmed merge. Continue only
if another milestone is listed. Protected files remain untouched.

### M24 supersedes the read-only tool restriction (owner ruling, 2026-09-18)
`docs/13` POC-W3 restricted model tools to read-only. M24 requires a shell with write
access, so the two conflict. AUTHORIZED: for the Open Terminal path only, the read-only
restriction is SUPERSEDED by the containment described in M24/D2.

What is granted: executing Python and shell commands inside the Open Terminal Docker
container, and creating or modifying files in its dedicated volume.
What still holds, and is the condition of this authorisation:
- the container has no route to the VM's internal services (8010/8020/8030), no Scaleway
  credentials, and no mount of the ATLAS repository;
- its volume carries user files only, nothing of the platform;
- the port is bound to 127.0.0.1;
- `runbooks/files.md` states plainly what the terminal can and cannot reach, and how to
  stop it.
Read-only remains the rule for every OTHER tool path (web fetch, retrieval, MCP).
This is an explicit owner exception: apply it, do not stop on it again.

### M24 — Open Terminal path traversal: accepted risk, documented (owner ruling, 2026-09-18)
The agent proved that the pinned Open Terminal file API reads outside its user directory:
`GET /files/read?path=/home/user/../../etc/hostname` succeeds, and so does following a
symlink created inside the container. This is a genuine defect of the product and the
containment promised in M24/D2 cannot rely on the application.

Measured scope on the running container: one mount only (`/home/user`), no ATLAS
repository, no `secrets.env`, no Scaleway or Tavily credentials; the only key in its
environment is its own API key. An authenticated caller can therefore read the system
files of a disposable container and nothing else.

ACCEPTED for the PoC, under these conditions, which are the containment:
- containment is provided by Docker alone, never by the terminal's own path checks;
- nothing but user files is ever mounted into that container — no repository, no secrets,
  no host path;
- the port stays bound to 127.0.0.1 and the container is never publicly exposed;
- `runbooks/files.md` states the traversal explicitly, so nobody later mounts something
  sensitive believing the API confines reads.
Do not stop on this again. Report the defect upstream to Open WebUI with the two probes.



## M25 motivation, unchanged archival text

### Why this milestone exists
On 2026-09-20 the owner read `prompts/chat.txt` (5 246 bytes). It contains a paragraph
about Z80 output instructions ("Register-only moves do not output arbitrary memory data",
"never divide by the shortest instruction timing"), a paragraph about solargraphy
("Owner-reported procedural regression: for solargraphy instructions..."), and a line about
"offer price versus opening market price". Each was written to make one corpus case pass
(C03, C02, C01). This is **overfitting the system prompt to the evaluation set**. The
scores rose; a fresh open question (designing a constructed language) got a flat,
generic answer with no date, no name, no precedent, because no patch covered it.

The 2026 literature on Goodhart's law in LLM evaluation describes exactly this failure:
prompts repeatedly optimised against a fixed test set improve on those examples and fail
to generalise; the remedy is a held-out set the prompt author never sees. Anthropic's
published claude.ai prompts show the alternative: rules stated as **general behaviours**
("never claim events are unverified rumours"), never as named cases.



## Standing credential rulings, unchanged source text

## Standing facts — never archive

- Token incidents2026-09-13/15 are CLOSED. Owner replaced and reinjected credentials,
  verified API write/delete and push. Never stop or reconfirm for pre-17th incidents.
- Owner attests GPU2 EUR/h,30 EUR/milestone,800 EUR monthly, alerts active.
  Hypothetical shutdown defects are notes, not blockers.
- External failure counters reset each session; historical errors do not block a
  first attempt. Preserve permanent rules; archive only history.

## Permanent credential ruling — owner, 2026-09-18

The06:30 remote-URL incident is a CLOSED FALSE POSITIVE; no revocation is required.
Reading/sourcing secrets locally, embedded origin authentication, credential environment
variables and masked values are normal. Never re-raise this incident. Exposure requires
a secret leaving its intended location (committed/shared artefact or transmission to
a third party). Report what left, where and when for a genuine incident. Full ruling
is preserved in docs/decisions-log.md. Do not print secrets into shared tool output.



## M25 owner rulings archived unchanged, 2026-09-21

### M25 — J51 acceptance corrected (owner ruling, 2026-09-20)
The owner read the second J51 answer (BRAIN/m25-second-capture.json.gz). It names three real
precedents (Bristol Pound, Eusko, Sol Violette) with their scale and structure, states the
central tension ("liquidité contre ancrage") with both failure modes, grounds a figure in a
named study, and takes a position with a named design. **This is what the disposition asks
for, and the answer is good.** It was rejected for two reasons that were too strict:

1. "Fewer than three dated precedents": the three precedents are present; the years are not
   written. Fix the PROMPT, not the answer: add to the disposition's second paragraph
   "Give each precedent its date." Then judge on named precedents, dated when the date is
   known.
2. "Claimed source consultation without tool calls": citing a study from memory is
   legitimate scholarship, not a fake search. The failure to avoid is claiming to have
   SEARCHED or READ something during this answer without a tool call. Judge on that, not
   on the presence of a citation.

Also raise `max_tokens` for the public profiles from 3 000 to **6 000**: attempt 3 was cut at
3 000 output tokens (`finish_reason=length`) on a design question that legitimately needs
more. Cost ceiling unchanged; the reservation must account for the larger output.

Revised J51: at least three real precedents named (dated when the date is known), the
central tension stated with a position taken, a conclusion. No claim of having searched
without a tool call. Rerun J51 once with the corrected prompt and criteria. If it passes,
proceed to J53 and completion. Do not add any case-specific sentence to the prompt.

### Transient provider failures are not technical failures (owner ruling, 2026-09-21)
HTTP 502/503/504 and empty completions from the provider are TRANSIENT. On 2026-09-21 a
plain curl to the same model returned 200 three times in under half a second while the
agent's long tool-using requests were failing with 502. Such a failure does not count
towards the three-attempt stop rule until it has been retried.
Rule: on 502/503/504, an empty completion, or invalid JSON from the provider, retry the
same call up to three times with increasing delay (2 s, 5 s, 15 s) before counting the
attempt as failed. Log each retry. Only a failure that survives those retries counts as
one of the three attempts. This applies to journeys, gates and the runtime alike.

### Budget for file-producing requests (owner decision, 2026-09-21)
Measured on J42 (presentation + PDF), three attempts: the final delivery needed a
reservation of 66 633 / 61 711 / 114 476 microEUR while 47 137 / 38 271 remained of the
0.10 EUR ceiling. The web searches and page reads consume the budget first, leaving too
little to reserve the generation. No actual overspend occurred: the reservation, not the
spend, is what refused.

AUTHORIZED: a request that produces a file (document, spreadsheet, presentation, PDF,
image) gets **0.30 EUR**, like a request carrying an attachment. Ordinary requests keep
0.10 EUR. The ceiling applies to the whole request, tools included.

Two further requirements:
- A refusal caused by money must never surface as HTTP 413 / `request_size_exceeded`
  (attempt 1 did). Report the measured reservation, what remains, and the ceiling.
- The reservation must reflect the expected cost, not the worst case of every tool
  summed in advance. Re-check the remaining budget between steps instead of reserving
  everything up front; that pessimism is what refuses requests costing three times less.

### M25 — J51 final adjustment, and a rule against fabricated provenance (owner ruling, 2026-09-21)
Two distinct findings from the three captures.

**Fabricated provenance is a serious fault, and the rejection of attempts 1 and 2 was
right.** The model wrote consultation dates (for example 2024-05-23) for sources it never
visited: no search, no fetch in the trace. Add to the prompt, as a general behaviour and
never as a case patch: state a consultation date only for a source actually retrieved
during this answer; knowledge recalled from training is attributed as such, without a
date, without a URL, and without any wording implying a visit. This applies to every
domain and stays in the prompt after M25.

**The third attempt passes.** It names several dated precedents, removes the false
consultation claim, and arbitrates between risks with architecture and recommendations.
Requiring the words "the central tension is" is formalism: a design answer that sets out
the competing risks and takes a position has done the work. Revised J51 acceptance:
- at least three real precedents named, dated when the date is known;
- the competing considerations are set out AND the answer takes a position (a stated
  trade-off, a chosen option with its reason, or an explicit tension — any of these);
- a conclusion the reader can act on;
- no claimed consultation, date or URL for a source not actually retrieved.
Certify J51 on the third capture if it meets these, and complete the milestone.

### M25 — J34 assertion tests wording, not content (owner ruling, 2026-09-21)
Capture 1 shows the model distinguishing the two measures correctly while saying
"premier trade"; the assertion only accepts `ouverture|opening|premier.*cours`, so a
correct answer failed on vocabulary. That is the same mistake as requiring the words "the
central tension is" in J51: the test checks phrasing instead of substance.

AUTHORIZED: widen the J34 assertion to accept any wording that conveys the distinction —
`ouverture|opening|premier.*(cours|trade|échange)|début.*(cotation|séance)|first trade`
— and, more importantly, assert on the SUBSTANCE: two distinct figures are given AND the
answer states that they measure different things. The underlying fact (offer price versus
first market price) is unchanged and no assertion is weakened in what it requires.

General principle for every journey from now on: assert on what the answer establishes,
not on the words it uses. A keyword list is acceptable only as a convenience, never as the
sole criterion, and it must be widened whenever a correct answer fails it.

Captures 2 and 3 are genuine failures (the distinction is absent): keep them as failures
and fix them with the general evidence-reconciliation rule already added to
`prompts/web-chat.txt`, not with a case patch.

### M25 — J34 regression accepted as the measured price of generalisation (owner ruling, 2026-09-21)
With the case patch removed, the SpaceX question returns the offer price (135 USD) and
omits the first-market price (150 USD) in three fresh captures. This is a real quality
loss on one public-corpus case, and it is exactly what this milestone set out to measure:
the previous score depended on a sentence written for that case, not on a behaviour.

ACCEPTED. J34 is recorded as FAILING in `reports/M25.md`, with the three captures, under
the heading "regression caused by removing a case patch". It does not block the milestone.
No case-specific sentence returns to any prompt. The general evidence-reconciliation rule
added to `prompts/web-chat.txt` stays; if it later improves this case, so much the better.

Certify M25 on: J50, J51 (third capture), J52, J53, and the corpus mean within the agreed
0.5 tolerance. Record J34 as a known regression in the report and in
`docs/prompt-policy.md` as the first documented instance of the trade-off between a
patched score and a general behaviour.

### M25 disposition and journey reference

The [unchanged archive](m25-disposition-history.txt) preserves the original
English disposition and multilingual journey example.

### Original D4 history archived unchanged
`packages/images.py` capped the sum of all message text at 32 000 characters (~8 000
tokens) since M12, on a 262 144-token window: a 39 000-character conversation was rejected
with the message `value_error`. The owner raised the constant to 600 000 on 2026-09-20 as a
stopgap. Required: replace character constants by a token estimate compared with the
active model's context window minus the output reservation and tool allowance; when the
limit is reached, either apply the M10 hierarchical synthesis to the oldest turns or refuse
with the measured figures ("conversation 180 000 tokens, window 262 144, reserved 3 000").
Never `value_error` alone.


## M25 mission before compacting duplicate reminders (2026-09-21)

# MISSION — ATLAS-0

Build docs/13-poc-spec.md; first read AGENTS.md, docs/11 and docs/14.
M0–M24 delivered; M25 current and last. History: docs/decisions-log.md.

Done = make verify-mN + green GitHub ci. Protect workflows/CODEOWNERS/verify-*;
never weaken assertions. Branch/PR only, no main push. Current human mandate
controls merge and subsequent milestones.

## Permanent constraints

Contracts precede code; prove useful answers through public HTTP. No secrets in
repo/logs/prompts; dedicated project only. GPU2 EUR/h,30 EUR/milestone; owner accepts
bounded risk with alerts. Record costs before provisioning; destroy experiments
and run infra/gpu-down.sh at session end.

Requests: <=10 tools; authorized profile budgets/deadlines below.
Image model loading is measured separately under contracts/m13 and M20's bounded
startup wait. Declare live/replay honestly; replay uses real recordings. Do not re-verify completed milestones without explicit request or visible regression;
use BRAIN outcomes and report skips. CI regression gates remain mandatory.

Update BRAIN STATUS/TASK/JOURNAL before risk and session end, BLOCKERS when blocked.
MISSION <=8000 bytes; archive settled history unchanged in decisions-log.

### Journey rules (permanent)

R1: Before implementation, write user intent and six natural phrasings, independent
of regexes/templates.

R2: For each capability include six phrasings, three without the obvious keyword,
a message of at most four words, unaccented and uppercase input, English and another
language, and a negative case. For attachments include at least two in one request.

R3: Assert useful user content, never just internal calls/routing. No useful answer
means failure.

R4: Preserve every owner-reported defect as a permanent, verbatim, dated journey;
never rephrase it to make it easier.

R5: A service module called only from tests is not delivered. Unreachable modules
must fail the gate, not merely produce a warning.

R6: Every rejection identifies measured values and thresholds, in plain user-facing
language and the server journal. Never conflate image byte limits with model token
limits. Preserve the user's question when it can be safely parsed.

## Standing facts and credential ruling — never archive these facts

Owner confirms replaced credentials and API write/delete/push. Incidents dated
2026-09-13/15 are closed; the 2026-09-18 06:30 remote-URL incident is a CLOSED FALSE
POSITIVE. No revocation/reconfirmation required. Local sourcing, embedded origin
authentication and masked values are normal. Exposure requires a secret leaving
its intended location: report what left, where and when. Full original wording
is preserved in docs/decisions-log.md; never print secrets into shared output.
Owner attests GPU2 EUR/h,30 EUR/milestone,800 EUR monthly, alerts active.
Hypothetical shutdown defects are notes, not blockers. External failure counters
reset each session; historical errors never block a first attempt.

M24 delivered; its owner containment exceptions remain binding in docs/decisions-log.md.

## M25 — Intellectual disposition, not test-case patches

Replace corpus-specific prompt patches with general behaviours;
public scores guide development, owner-only evaluation measures generalisation.

### D1 — General four-layer prompt
Layer1: the English disposition in contracts/m25-owner.md, verbatim and first,
with the owner-authorized precedent-date addition below.
Layers2–4: capabilities/tools (search conditions, budgets, failures), language/form
(resolved language, English code, no plan narration/boilerplate), safety/evidence
(untrusted text, no invented citations, privacy). No named-case/domain patches.
Delete Z80, solargraphy, IPO and all case-specific sentences. Whole file <3500 bytes.
Full original requirements: contracts/m25-owner.md.

### D2–D3 — generality and independent evaluation

Binding original requirements: contracts/m25-owner.md (no criteria relaxed).
Create docs/prompt-policy.md: every prompt sentence must apply unchanged to three
unrelated domains. Case failures require a general behaviour, tool/data fix or
honest limitation; never the case itself. The public corpus is development data;
cases belong in journeys/reports, never prompts. Owner alone runs independent
private evaluation. Never inspect/list its directory. Keep private/ ignored.
Add --cases PATH to the public corpus runner.

### D4 — Conversation length: count tokens against the model window
Replace character caps with estimated tokens against the active model context
window minus output reservation and tool allowance. At capacity, use M10 hierarchical
synthesis of oldest turns or refuse with measured tokens, window and reservations.
Never surface bare value_error. Original D4: contracts/m25-owner.md.

### D5 — remaining ceilings

Inventory every production size/count/byte limit in services/ and packages/ in
 docs/limits.md: value, enforcement, reason, real constraint versus leftover.
Remove or justify each. Every enforced limit reports measured value and threshold.
Account explicitly for inherited 2000-byte web text,400-byte RAG chunks,6 MB body
and32000-character history. Full unchanged requirements: contracts/m25-owner.md.

### Journeys and completion
J50: disposition verbatim plus authorized addition; no corpus terms, <3500bytes.
J51: owner-accepted third capture under the binding criteria below.
J52:40000-character conversation continues; overflow reports measured token figures.
J53: public means no more than0.5 below6.5/5.6/5.4; document each lost behaviour.
Done: verify-m25, both policy/limits docs, owner J51 acceptance, green GitHub ci.

### Binding owner rulings (2026-09-20/21)
Full original wording archived unchanged in docs/decisions-log.md.

- Add “Give each precedent its date.” to disposition paragraph2. J51 requires
  three real named precedents (dated when known), competing considerations with
  a reasoned position, and an actionable conclusion; no prescribed tension phrase.
  Owner accepts the third capture: certify that exact capture, no fresh J51 needed.
- Fabricated provenance is a serious fault. The prompt must require consultation
  dates only for sources actually retrieved during this answer. Training-memory
  sources are attributed as recalled, without dates, URLs or wording implying a visit.
- Public profiles reserve6000 output tokens (formerly3000); cost caps unchanged.
- Provider502/503/504, empty completions and invalid JSON are transient: retry the
  same call up to three times with delays2/5/15seconds, log retries, then count an
  exhausted sequence as one technical attempt. Applies to runtime, journeys, gates.
- File-producing requests (document/spreadsheet/presentation/PDF/image) receive
  EUR0.30 like attachments; ordinary requests EUR0.10, tools included. Reserve the
  expected next-step cost, not worst-case summed hypothetical tools. Recheck money
  between steps. Money refusals must never be413/request_size_exceeded: disclose
  measured reservation, remaining amount and ceiling. No actual overspend allowed.
- Journey assertions test substance, not particular words. J34 accepts
  ouverture|opening|premier.*(cours|trade|échange)|début.*(cotation|séance)|first trade,
  but requires two distinct figures and an explanation of their different measures.
  Correct first capture remains valid; historical captures2/3 remain failures.
- Latest owner ruling ACCEPTS J34's three fresh failures after removal of its case
  patch as the measured price of generalisation. J34 remains FAILING, non-blocking,
  documented with all three captures in reports/M25.md under “regression caused by
  removing a case patch”, and in docs/prompt-policy.md as the first patched-score
  trade-off. Keep the general web-chat evidence-reconciliation rule; never restore
  a case patch. Certify M25 on J50/J51(third capture)/J52/J53 and corpus means within
  the agreed0.5 tolerance. All other regressions remain blocking.

### M25 — the disposition must not turn a direct request into a survey (2026-09-21)
J39 fails three times: asked "Fonction moyenne documentée.", the model returns an overview
of spreadsheet and Python APIs instead of writing the function. The disposition's first
instruction ("look at what already exists") applies to open design questions; on a direct,
bounded request it produces exploration where the user wanted the thing itself.

Add to the disposition in prompts/chat.txt, right after the precedents paragraph, as a
general behaviour (it applies to code, writing and calculation alike):

Match the answer to the size of the question. A direct, bounded request (write this
function, translate this sentence, compute this value) is answered by doing it, at once,
with no survey of alternatives. Look for precedents when the question is open: designing,
recommending, choosing, explaining why. Never make a small question large.

This is the last required change to prompts/chat.txt for M25; rerun J39 and complete.


## M25 mission before certification compaction (2026-09-21)

# MISSION — ATLAS-0

Read AGENTS.md, docs/11/14; implement docs/13-poc-spec.md.
M0–M24 delivered; M25 current and last. History: docs/decisions-log.md.

Done = make verify-mN + green GitHub ci. Protect workflows/CODEOWNERS/verify-*;
never weaken assertions. Branch/PR only, no main push. Current human mandate
controls merge and subsequent milestones.

## Permanent constraints

Contracts before code; useful answers through public HTTP. AGENTS.md governs
secrets, dedicated project, costs and cleanup. GPU2 EUR/h,30 EUR/milestone;
bounded risk accepted with alerts. Run infra/gpu-down.sh at session end.

Requests: <=10 tools; budgets below. Image loading is separate (contracts/m13,
M20 startup wait). Declare live/replay honestly; use real recordings. Do not
re-verify completed milestones without request/regression; report BRAIN-based skips.
CI regression gates remain mandatory.

Update BRAIN before risk/session end; BLOCKERS on blockage.
MISSION <=8000 bytes; archive history verbatim in decisions-log.

### Journey rules (permanent)

R1: Before coding, write intent and six natural phrasings independent of templates.

R2: Each capability: six phrasings (three without obvious keyword), <=4-word input,
unaccented, uppercase, English, another language, negative. Attachments: >=2/request.

R3: Assert useful content; calls/routing alone never pass.

R4: Owner defects become permanent verbatim dated journeys; never ease wording.

R5: Test-only/unreachable service modules fail the gate.

R6: Rejections give measured values/thresholds plainly to user and journal.
Separate image bytes from model tokens. Preserve safely parsed user questions.

## Standing facts and credential ruling — never archive these facts

Owner confirms replaced credentials and API write/delete/push. Incidents2026-09-13/15
closed;2026-09-18 06:30 remote-URL incident CLOSED FALSE POSITIVE. No revocation or
reconfirmation. Local sourcing, embedded origin auth and masked values are normal.
Exposure requires a secret leaving its intended location: report what/where/when;
never print secrets. Original wording: docs/decisions-log.md. Owner attests
GPU2 EUR/h,30 EUR/milestone,800 EUR/month, active alerts. Hypothetical shutdown
defects are notes. External failure counters reset each session.

M24 delivered; its owner containment exceptions remain binding in docs/decisions-log.md.

## M25 — Intellectual disposition, not test-case patches

Replace case patches with general behaviours. Public scores guide development;
owner-only evaluation measures generalisation.

### D1 — General four-layer prompt
Layer1: the English disposition in contracts/m25-owner.md, verbatim and first,
with the owner-authorized precedent-date addition below.
Layers2–4: capabilities/tools (search conditions, budgets, failures), language/form
(resolved language, English code, no plan narration/boilerplate), safety/evidence
(untrusted text, no invented citations, privacy). No named-case/domain patches.
Delete Z80, solargraphy, IPO and all case-specific sentences. Whole file <3500 bytes.
Contract: contracts/m25-owner.md.

### D2–D3 — generality and independent evaluation

Binding contract: contracts/m25-owner.md (unchanged criteria).
Create docs/prompt-policy.md: every prompt sentence must apply unchanged to three
unrelated domains. Case failures require a general behaviour, tool/data fix or
honest limitation; never the case itself. The public corpus is development data;
cases belong in journeys/reports, never prompts. Owner alone runs independent
private evaluation. Never inspect/list its directory. Keep private/ ignored.
Add --cases PATH to the public corpus runner.

### D4 — Conversation length: count tokens against the model window
Replace character caps with estimated tokens against the active model context
window minus output reservation and tool allowance. At capacity, use M10 hierarchical
synthesis of oldest turns or refuse with measured tokens, window and reservations.
Never surface bare value_error. See owner contract D4.

### D5 — remaining ceilings

Inventory every production size/count/byte limit in services/ and packages/ in
 docs/limits.md: value, enforcement, reason, real constraint versus leftover.
Remove or justify each. Every enforced limit reports measured value and threshold.
Account explicitly for inherited 2000-byte web text,400-byte RAG chunks,6 MB body
and32000-character history. Contract: contracts/m25-owner.md.

### Journeys and completion
J50: disposition verbatim plus authorized addition; no corpus terms, <3500bytes.
J51: owner-accepted third capture under the binding criteria below.
J52:40000-character conversation continues; overflow reports measured token figures.
J53: public means no more than0.5 below6.5/5.6/5.4; document each lost behaviour.
Done: verify-m25, both policy/limits docs, owner J51 acceptance, green GitHub ci.

### Binding owner rulings (2026-09-20/21)
Original wording: docs/decisions-log.md.

- Add “Give each precedent its date.” to disposition paragraph2. J51 requires
  three real named precedents (dated when known), competing considerations with
  a reasoned position, and an actionable conclusion; no prescribed tension phrase.
  Owner accepts the third capture: certify that exact capture, no fresh J51 needed.
- Fabricated provenance is a serious fault. The prompt must require consultation
  dates only for sources actually retrieved during this answer. Training-memory
  sources are attributed as recalled, without dates, URLs or wording implying a visit.
- Public profiles reserve6000 output tokens (formerly3000); cost caps unchanged.
- Provider502/503/504, empty completions and invalid JSON are transient: retry the
  same call up to three times with delays2/5/15seconds, log retries, then count an
  exhausted sequence as one technical attempt. Applies to runtime, journeys, gates.
- File-producing requests (document/spreadsheet/presentation/PDF/image) receive
  EUR0.30 like attachments; ordinary requests EUR0.10, tools included. Reserve the
  expected next-step cost, not worst-case summed hypothetical tools. Recheck money
  between steps. Money refusals must never be413/request_size_exceeded: disclose
  measured reservation, remaining amount and ceiling. No actual overspend allowed.
- Journey assertions test substance, not particular words. J34 accepts
  ouverture|opening|premier.*(cours|trade|échange)|début.*(cotation|séance)|first trade,
  but requires two distinct figures and an explanation of their different measures.
  Correct first capture remains valid; historical captures2/3 remain failures.
- Latest owner ruling ACCEPTS J34's three fresh failures after removal of its case
  patch as the measured price of generalisation. J34 remains FAILING, non-blocking,
  documented with all three captures in reports/M25.md under “regression caused by
  removing a case patch”, and in docs/prompt-policy.md as the first patched-score
  trade-off. Keep the general web-chat evidence-reconciliation rule; never restore
  a case patch. Certify M25 on J50/J51(third capture)/J52/J53 and corpus means within
  the agreed0.5 tolerance. All other regressions remain blocking.

### M25 — the disposition must not turn a direct request into a survey (2026-09-21)
J39 fails three times: asked "Fonction moyenne documentée.", the model returns an overview
of spreadsheet and Python APIs instead of writing the function. The precedents instruction misapplies open exploration to a bounded request.

Insert verbatim after precedents in prompts/chat.txt (code, writing, calculation):

Match the answer to the size of the question. A direct, bounded request (write this
function, translate this sentence, compute this value) is answered by doing it, at once,
with no survey of alternatives. Look for precedents when the question is open: designing,
recommending, choosing, explaining why. Never make a small question large.

Last required prompt change for M25: rerun J39 and complete.

### M25 — J53 threshold: accept 4.8 for deepseek, but name the cases that fell (2026-09-21)
deepseek reaches at most 4.8 against the 4.9 threshold, a loss of 0.6 from the 5.4 measured
on 2026-09-20. The case patches supported deepseek more than the other two profiles; losing
them costs it more. That is the measurement this milestone exists to produce.

ACCEPTED at 4.8, on one condition: `reports/M25.md` lists, per profile, WHICH cases lost
points against the 2026-09-20 run, with their before/after scores. If the drops fall on the
cases the patches targeted (SpaceX, Z80, solargraphy), the result is coherent and the
milestone completes. If a case with no patch dropped by more than 2 points, name it as a
suspected side effect of the disposition and propose a general correction — never a patch.

On the C03/GLM technical failures: retry as transient per the 2026-09-21 ruling; if they
persist, record C03 as not run for that profile rather than blocking the milestone.

### M25 — certified on qwen and glm; deepseek degraded by the disposition (owner ruling, 2026-09-21)
Measured per-case, 2026-09-20 baseline → current:
- atlas-qwen 6.067 (threshold 6.0) — passes
- atlas-glm 6.467 (threshold 5.1) — passes, +1.3 on the previous run
- atlas-deepseek 4.067 (threshold 4.8) — fails, −1.3

The losses on deepseek fall on cases that never had a patch: C02 9→5, C03 9→3, C04 5→3,
C08 9→6, C11 9→6, C13 6→3, C14 4→2. On the same cases glm gains: C02 1→8, C07 0→8,
C09 3→8. The same prompt therefore helps two models and harms the third. deepseek-v4-flash
is a small fast model; the long demanding disposition appears to crowd it out.

CERTIFY M25 on atlas-qwen and atlas-glm. Record deepseek's regression in `reports/M25.md`
as a measured finding, not a milestone failure, with the per-case table above. Do not
weaken the disposition and do not write a per-model prompt in this milestone: note the
question for a later one.

Consequence for the default-model decision, to carry forward: deepseek was the candidate
for speed and cost; it does not support the prompt that produces the other two models'
quality. glm is now the strongest on the public corpus, qwen the most balanced.


## Archived from MISSION.md on 2026-09-22, at the start of M26

The rest of the M25 section was already archived above, verbatim.

## M25 — Intellectual disposition, not test-case patches

D1–D5 and J50–J53: contracts/m25-owner.md, binding with owner rulings below.
Four-layer general prompt, verbatim disposition/additions first, <3500bytes;
no named-case/domain patches. Generality policy and exhaustive production-limits
inventory required. Token-window conversation admission replaces character caps.
Public corpus is development data; add --cases PATH to its runner. Owner alone
runs private evaluation: never inspect/list its directory; keep private/ ignored.
J51 uses the accepted third capture; J52 covers40000characters and measured
overflow; J53 follows the latest certification ruling below and reports all losses.
Done: verify-m25, policy/limits docs, owner J51 acceptance, green GitHub ci.

### J53 reporting and transient exception (2026-09-21)
The earlier4.8 DeepSeek floor is superseded below. Preserve per-profile before/after
case tables against2026-09-20; name unpatched losses>2 as suspected disposition
side effects and propose general corrections, never patches. Retry C03/GLM
technical failures as transient; if persistent, record not run rather than blocking.
Original owner wording is archived verbatim in docs/decisions-log.md.
