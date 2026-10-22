# Orchestrator M3

Owner: OWNERS. Contract: [M3](../../contracts/m3.md).
REQ-HAR-001/002/006, REQ-TOOL-001/002/004/012/013/014/015, REQ-FIN-002.

The model selects tools, the harness validates arguments and reserves cost/tokens
before each I/O. Ceilings trigger a `stopped` result with an explicit reason;
tool errors are returned to the model for bounded recovery.
Web/RAG results are untrusted; instructions remain in prompts/.

## Local Operation

From the root, with the Python environment installed:
`uvicorn services.orchestrator.app:app --host 127.0.0.1 --port 8020`.
The M2 CPU/serverless gateway must run at `ATLAS_GATEWAY_URL` (default port 8010),
and `ATLAS_RETRIEVAL_DSN` must point to the ingested M2 database. The gateway receives its
credentials via environment; `SERPAPI_KEY` remains in the harness environment.
`ATLAS_WEB_CACHE` designates SQLite (default BRAIN/web-cache.sqlite).
Do not expose this server directly without authentication; M4 UI integration.

POST `/query`: `{"question":"Combien font 7*9 ?","lang":"fr"}`.
GET `/continuation/{handle}?offset=0` reads subsequent excerpts until expiration.
Process termination via SIGTERM; no GPU required.

M3 SLO: request stop <=120s, <=10 tools, <=0.05 EUR, <=16384 tokens.
Reservations remain counted if the call fails or expires; no promise that HTTP
cancellation cancels provider billing. SerpApi cache 1h, pages 24h,
reserved quota 900/month; retain SQLite across restarts to preserve quota.

Diagnostics: examine `state`, `reason`, `tokens`, `cost`, `tool_calls`, `trace`.
JSON events `tool_finished` and `request_stopped` do not log credentials or request content.
M3 operator view: these counters per response and `BRAIN/eval/{tools,web}.json` per language;
aggregated dashboard falls under M5.
A robots/SSRF refusal cannot be bypassed. After three identical errors, log
BRAIN/BLOCKERS.md and stop; do not clear quota to unblock a search.

## Verification

`make test-web-security test-budgets`: local HTTP, DNS, robots, size and budgets.
`make verify-m3`: real E4/E6, E4 faults injected into the driver under tests/.
`ATLAS_M3_EVAL_MODE=replay make verify-m3`: strict replay of the same calls;
a modified prompt or different calls invalidate cassettes.
No mocks are imported by the runtime. Cassettes are exclusively under tests/.
E6 measures executability here with URL/date; no quality GO without key review
and human calibration of the judge. The fidelity of each sentence falls under gate E5.

M7: chat_pipeline uses harness budgets, HTTP retrieval, and resolved citations.
interactions writes received measurements and stop codes in private JSONL;
full contract and limits documented in contracts/m7.md.

M12: `packages/images.py` defines valid image messages from contract `contracts/m12.md`.
PNG/JPEG are decoded by Pillow after dimension checks; remote URLs,
unsupported formats, and cumulative overruns are rejected. This first
building block is common to future vision adapter/gateway boundaries.
The chat now accepts `text` and `image_url` parts in PNG/JPEG data URI.
Any image in history selects `/vision/complete`; text exchanges
retain the harness. The log contains metadata, tokens/cost, and vision route,
without base64, even if the provider echoes bytes back. Budget and timeout errors
remain distinct and retain a 0.05 EUR reservation.

Vision relays its complete validated response via SSE; it does not claim
to produce upstream progressive tokens. Text/project retains M11 transport.

M15: developer entry `PYTHONPATH=.:services/model-gateway uvicorn services.orchestrator.devapi:app --host 127.0.0.1 --port 8030 --no-access-log`; keys via `python -m services.orchestrator.dev_auth create <dev> --key-file <fichier-privé>`, revocation via `revoke <dev>`. ATLAS_DEVAPI_DB retains daily quotas and unknown reservations; contract contracts/m15.md. POST /v1/chat/completions now exposes text and functions in JSON or SSE, without access to private chat tools/data.

M15 Chat Protocol: dev_chat normalizes text messages and assembles deltas without executing functions. Identifiers must remain stable, complete arguments must be finite JSON objects, and names must come from declared tools. Provider stop with complete call becomes tool_calls; length remains a truncation.

M10 Selection: relevance contributions are summed using math.fsum to preserve ties regardless of Python hash seed. Existing tie-breaking by position remains deterministic; the exact provider cassette is unchanged.

M15: dev_input translates Responses/Messages histories to the same validated messages, without storing conversations. Metadata and cache are hints with no identity effect or cache guarantee; Messages effort without thinking does not activate it. Images, hosted tools, Responses storage, and thinking Messages are explicitly rejected.

M15: POST /v1/responses provides named text/function events, item identifiers, and completed/incomplete termination; store=false. The same quota is reserved before the stream and reconciled against provider usage.

M15: POST /v1/messages translates text/tool_use/tool_result and SSE message/content_block events. A truncated output remains max_tokens; costs come from the same registry per key. Experimental compatibility for clients configured without thinking or hosted tools.

M15: make test-devapi verifies the six gate criteria with recorded real streams. The large context case transmits 24,616 bytes of useful functions and verifies three remote values; double is rejected before inference for budget. The registry retains known costs and unknown reservations per key.

The chat adapter also mounts the bounded M3 harness at `/harness`: POST
`/harness/query` and GET `/harness/continuation/{handle}`. These existing
read-only endpoints retain their validation and budgets.

## Code mode backend (owner instance only)

REQ code-mode-core implements the owner scope of 2026-10-20. Enable with
`ATLAS_CODE_MODE=1`; `ATLAS_STABLE=1` always disables it. POST the normal
`/v1/chat/completions` request with `code_mode: true`, a UUID `code_session_id`,
and initially one zip/tar as an existing user-message file part:
`{"type":"file","file":{"filename":"project.zip","file_data":"BASE64"}}`.
The adapter normalizes it into internal `documents`; direct public `documents`
fields are ignored by the existing upload boundary. Further turns reuse that
session without an attachment. Reimport into an
initialized session is rejected. The gateway's existing public profile mapped to
its `code` role selects the existing CODE_MODEL configuration; no provider name
is introduced here.

The model receives the actual workspace listing and `archive_state: already_extracted` before its first call. File tools accept paths relative to the workspace or absolute paths under `/workspace`; archive entries must always be relative.

The stream uses ordinary OpenAI SSE envelopes. `atlas.code` is a versioned event:
`{"version":1,"session_id":"UUID","turn_id":"chatcmpl-...","seq":1,
"type":"tool","tool":"read_file","label":"read_file","status":"running",
"output":{}}`. Sequence numbers increase within a turn. Tool status is `running`,
`completed`, or `failed`; completion carries the actual bounded, redacted result
in `output`. Types `workspace`, `import`, `export`, `error`, `plan`, `approval`, `diff`, `limit`, `honesty`, and `recap` use
that same envelope. Plan output contains `items` with `text` and status `pending`,
`in_progress`, or `completed`. Approval output contains an opaque `approval_id`,
`action`, and `timeout_seconds`; POST `{"allow":true}` or `{"allow":false}` to
`/code/approvals/{approval_id}`. Denial or expiry prevents the gated operation.
Diff output carries `diff`. Limit output has `kind`, `measured`, `limit`, and
`unit`. Recap output contains the checked final `text`, steps and cost.

Code events bypass prose/narration/citation filters. Completed tool events also
supply escaped `<details><summary>tool — status</summary><pre>output</pre></details>`
in the normal content delta. Final model prose is held until the honesty guard
has checked the complete answer. Clients can render `atlas.code` independently
of this fallback. Approval is turn-local, single-use and canceled on disconnect.
The MVP offers no outgoing tools; a future sensitive tool must call the same gate
before its side effect. Existing stream cancellation stops the running container.
Concurrent turns for one session are rejected explicitly; the UI can queue them.

Defaults: `ATLAS_CODE_MAX_STEPS=40`, `ATLAS_CODE_WALL_SECONDS=1200`,
`ATLAS_CODE_COST_EUR=3`, `ATLAS_CODE_APPROVAL_SECONDS=120`.
`ATLAS_CODE_MAX_MODEL_CALLS=41` additionally bounds model-only loops. Model calls
retain the gateway's per-call ceiling and reserve cost before I/O. Stops are
normal checked replies with measured values, never HTTP 504 quota errors.

Workspaces persist in Docker volume `atlas-code-workspaces[-PORT_BASE]`, with one
UUID-derived directory per session. The terminal mounts this volume read-only;
code workers use the same pinned image in separate `--network none` containers,
mount only their session subdirectory, inherit no host environment, and have a
read-only root, no capabilities, no privileges, bounded memory/PIDs, and tmpfs.
All links and special archive entries are rejected, as are traversal, absolute
paths, imported Git metadata, more than 200 MB expanded data or 20,000 entries.
The existing attachment ingress has a smaller compressed-upload limit. A baseline
Git commit is recorded outside the workspace. Export preserves project contents
including Git metadata, rejects links/special files, and includes untracked edits
in the baseline patch. Downloads use the existing terminal file-serving URL.

Audit JSONL and baseline/plan state live outside Docker under
`ATLAS_CODE_STATE_DIR` (default `BRAIN/code-mode/state`), with owner-only file
permissions. GET `/code/sessions/{session_id}/audit` returns redacted JSONL.
Host credentials are never passed to workers; known credential values and common
credential patterns are redacted from tool results, audit and stream text.
Imported projects must contain no credentials. This is a local owner capability,
not a multi-tenant security boundary or a guarantee against arbitrary encoded
secrets deliberately embedded in source. Archives/patches are explicit owner
exports, not model tool results. The owner interface is described below.

Side view: `window.atlasSideView.open({kind, src, title, slot})` opens `html`, `url`, `pdf`, or `markdown`; `slot` is `top` (default) or `bottom`.
Use `content` for inline HTML/Markdown; `window.atlasSideView.close()` disposes both slots; Escape also closes the pane.
Development previews support `#atlas-view=url:<encoded URL>` and HTTP(S) localhost URLs; HTML uses an opaque sandbox with scripts only.
PDF/Markdown sources are same-origin; optional Markdown `saveTarget: "/api/v1/files/{id}/data/content/update"` saves extracted text, not original bytes; Download .md always preserves edits.
Vendored pdfjs-dist 4.10.38 (Apache-2.0, legacy build/worker) and marked 15.0.12 (MIT) under static/vendor avoid CDN traffic; Markdown is rebuilt through an HTML allowlist with raw HTML disabled.

### Code interface

The owned `chat-ui.js` overlay adds a Code entry beside the chat composer only
when the HTML relay injects `meta[name=atlas-code-mode]` for an enabled owner.
The backend flag check remains authoritative. Guest instances never inject the
marker and reject all `/atlas-code/` requests. The narrow same-origin relay maps
that prefix to the adapter for chat streaming, approvals, `/code/ui` (enabled
and public profile), and `/code/sessions/{UUID}/status` (running). It rejects
foreign origins and strips browser credentials; it is an owner loopback facility,
not a new public authentication boundary.

Import one zip/tar, then send a task. The workspace UUID and conversation are
kept per chat URL in this tab's session storage; subsequent turns reuse the UUID.
The archive bytes are sent only until the import event confirms extraction.
Reload retains activity and session identity but does not resume a stream or
reactivate old approval buttons. Server workspaces persist independently.
Activity uses version 1 events, with sequence deduplication per turn; fallback
HTML deltas are not rendered twice. Durations use client elapsed time, or the
measured tool elapsed time when supplied. Command blocks have Copy only: the
backend has no safe direct-command execution endpoint. Ask the agent to run a
command through its existing tool loop.

Stop aborts the fetch stream; the real pipeline cancels its worker and approvals
on disconnect. There is no separate abort endpoint. Before sending a queued
message the UI polls session status for release (up to 20 seconds); an unavailable
or still-busy session retains the queue with an explicit notice. The latest plan
is pinned during execution and retained in the final turn. Recap output adds
`question` and `actions` (one question, two next-message suggestions) alongside
the checked text and measured usage. Export URLs come only from export events.
Honesty corrections and measured limits remain inline in the transcript.

Side view also accepts `{kind: "diff", content: unifiedPatch, title, slot}`.
Each file displays plain-text before (`-`) and after (`+`) lines with context;
source HTML is never interpreted. Download saves the exact displayed patch.

`tests/code_ui_server.py` supplies a scripted model to the real pipeline and
Docker workspace for browser acceptance. It is test-only; production has no
replay switch. `tests/code_ui_browser.py` exercises the real Open WebUI shell
and captures owner states at 1280 and 1500 pixels. `--live` uses the unmodified
stack adapter, with the stack's model-call cap set to four for that run.
Long checked replies are collapsed behind “Full checked response”; the visible
recap uses two original declarative sentences, retaining the complete reply in
that disclosure and in conversation history. Short replies include the recorded
step count when a second sentence is needed. The single next-step question and
two answer buttons are rendered separately from export controls.

### Administrator image safety

`config/atlas-policy.json` is read once at process startup by both the orchestrator
and the image gateway. Set `ATLAS_POLICY_FILE` to an absolute JSON file path to
replace it. Restart both services after a change. The strict schema permits only
`image_safety_level`, whose values are `standard` (also the omitted-key default)
and `strict`. Unreadable files, unknown keys, non-object JSON and unknown levels
stop startup with a configuration error; rejected values are not logged.

Standard preserves artistic, historical, religious and medical imagery. Strict
also refuses nudity (including artistic/medical), gore and graphic violence.
The three hard limits in `packages/image_limits.json` always apply and cannot be
configured away. Refusals identify the rule in the request language and use the
existing rejection telemetry without copying the request into that rejection.
Future organization overrides must use `tighten`: strict system policy cannot be
lowered by a standard organization policy. Organization selection is not implemented.
