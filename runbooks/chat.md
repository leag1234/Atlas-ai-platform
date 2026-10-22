# Chat M7

Run `make serve` with Scaleway/SerpApi variables injected (`.env` optional).
Python: requirements-dev.txt; Docker Linux. No cloud credentials transmitted to the UI.
From your workstation: `ssh -L 3000:127.0.0.1:3000 -L 8020:127.0.0.1:8020 user@vm`,
then http://localhost:3000; select atlas. The 8020 tunnel serves citations.
Ctrl-C stops the services; atlas-chat-ui/index volumes are preserved. Private logs:
BRAIN/interactions/YYYY-MM-DD.jsonl. Current public chat budget/stop:120 s and0.10 EUR per request.
Local single-user UI; no HTTPS and no public exposure. No GPU created by serve.
Open WebUI v0.11.3, pinned digest, image ~7.14 GB; interface/branding preserved.
Dependency imposed by MISSION; LibreChat alternative discarded for a single integration.
Configuration/license source: https://docs.openwebui.com/reference/env-configuration/
and https://github.com/open-webui/open-webui/blob/v0.11.3/LICENSE .

Stop serve before `make verify-m7`: tests use the same local ports.
For background launch of the agent, the group PID is in BRAIN/m7-serve.pid.
The execution field distinguishes live, record (real test call), and replay (no cost).
Old lines without this field have an undetermined origin: do not sum their costs.


## Integrated chat journeys (M17)

Use `/project create <name>` in chat to create and select a project. In another
chat, use `/project use <name or ID>` to share its stored facts. Names must be
unambiguous. Each selection creates a separate conversation in that project.
The selection receipt carries the project and conversation IDs; preserve it in
chat history. `/project leave` clears selection. The existing project page at
`http://localhost:8020/project-ui` supports reviewing and deleting stored facts.
Project selection is per conversation, never a global server setting.
Project scope is resolved before image handling. Current uploads are described
using only server history from the selected conversation. The description is
stored there, without image bytes; resend an image to inspect it again. Client
history from previous projects (including images) is never forwarded in project mode.

Title, tags, follow-up and autocomplete auxiliary generation are explicitly
disabled because the PoC has no independent auxiliary budget. Persistent UI
configuration is disabled so these settings also apply to an existing UI volume.
Restart `make serve` to apply them; chat history in the volume is preserved.

Image generation needs a warm image GPU: run `make serve-imagegen` from a shell
with the project credentials before asking for an image. This explicit command
prepares the model and shuts the GPU down after ten minutes. `make serve` never
provisions a GPU. Generation requests retain the 0.05 EUR and 120-second caps;
GPU preparation is a separately budgeted infrastructure operation.

`make test-journeys` starts an isolated test stack and replays recorded external
responses through the real HTTP adapter. `JOURNEYS_LIVE=1 make test-journeys`
records real provider/web/image exchanges, requiring credentials and a warm image
GPU. Both write `BRAIN/eval/journeys.json`; do not run while the chat ports are in
use. Never label replay output as live evidence.

`make test-serve-idempotent` is separate from CI: it starts `make serve` twice and
checks readiness after the second start. Run only with no active user session.
The launcher requests graceful shutdown of its previous process and replaces
only its named containers with matching images; persistent volumes are retained.

## M21 model selection and evidence

The selector exposes atlas, atlas-glm and atlas-fast. All explicitly disable
reasoning and use3000 output tokens,120 seconds,0.10 EUR and10 tools. Changing the
model keeps the same prompts/tools. The gateway's text/code/fast roles are configured
centrally; unsupported vision uses a compatible alternate. Public response metadata
and request logs identify the provider that supplied the answer and its cost.

Activity reports elapsed time and names tools/URLs. Expand the native tool controls
to inspect steps; unexpected provider reasoning remains separate and collapsed.
Uploaded photos are stored once by reference; history does not accumulate base64.
Web/RAG evidence uses token budgets and discloses incomplete coverage.

Before emitting answer text, empty/malformed replies may recover once within the
same ledger. Failure after visible text is reported explicitly rather than appending
a second derivation. Errors report measured time and reservation limits. Never treat
an interrupted answer as complete or assume model availability proves accuracy.

Stop other disposable test stacks before M21 verification; they share ports.
`M21_LIVE=1 PYTHONPATH=.:tests:services/model-gateway python tests/m21_gate.py`
records real provider exchanges for J27–J33. `make verify-m21` checks the complete
journey report, including earlier regressions. Compare live/replay labels and the
quality breakdown in reports/M21.md; only green GitHub ci certifies completion.

## M23 attachments and readable answers

`make serve` installs the owned native inlet from `infra/openwebui_images.py`.
`BYPASS_EMBEDDING_AND_RETRIEVAL=True` disables attachment embeddings. The inlet
consumes attached file IDs before Open WebUI's retrieval handler, verifies ownership
using the authenticated UI user, reads original bytes from Open WebUI storage and
forwards standard `type=file` parts with `file.filename` and base64 `file.file_data`.
It never fetches client-supplied URLs. Do not disable the managed inlet: doing so
restores Open WebUI's own attachment processing. An unavailable original is an
explicit error, never a fragment fallback.

The adapter accepts PDF, DOCX, UTF-8 TXT, MD and CSV; at most eight documents,
16 MiB per file and 32 MiB per HTTP request. PDF text layers are required on every
page. Password-protected PDFs fail explicitly. The interaction journal records
filename, raw bytes, pages (zero when the format has no page model), extracted
characters and hierarchical processing. Documents bypass corpus/project retrieval.
The existing gateway retains its conservative context and cost reservations;
only context above262144 estimated tokens uses the M10 hierarchy. The estimator
never authorizes spending; a conservative gateway budget refusal remains possible. No extraction is
silently labelled complete after an error.

The default reading font is controlled by `--atlas-body-font` in chat-ui.css;
Open WebUI's user font preference still overrides it. Code scrolls horizontally,
copy appears on hover or keyboard focus, and both themes share readable spacing.
Visual acceptance remains an owner check on the original Python debugging answer.

Web replies expose the actual retrieved links in a collapsed section. External URLs
without matching retrieval are withheld from the answer.
