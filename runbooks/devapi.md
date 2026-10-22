# M15 Developer API

REQ-DEV-001..005; contract `contracts/m15.md`. `make serve-devapi` listens on
127.0.0.1:8030, accessible via SSH tunnel 8030. No GPU. Clients have no access
to conversations, RAG, or MCP tools from the private chat 8020.

Administer keys locally, using the same ATLAS_DEVAPI_DB variable as the server
(default BRAIN/devapi/usage.sqlite):

```sh
python -m services.orchestrator.dev_auth create alice --key-file /chemin/prive/alice.key --daily-requests 20 --daily-micro-eur 50000
python -m services.orchestrator.dev_auth revoke alice
```

The delivery file is created exclusively with mode 0600; no keys on stdout. Transmit
it via a private channel. Preserve SQLite between restarts: unknown reservations and
daily UTC quotas persist. GET /v1/usage exposes only the counter for the authenticated
key. Do not delete the database to reset a quota.

Public alias `atlas-code`, gateway code role; one attempt per request,
maximum 0.05 EUR reserved BEFORE the call. UTF8 bytes and the output maximum are
conservatively bounded: the financial capacity may be lower than the provider's window.
The validated case contains 24,616 bytes of useful code. Explicitly reduce context or
max_tokens if 400; no silent truncation. Insufficient daily quota returns 429,
missing/invalid/revoked key returns 401, provider error returns 502. After cancellation or missing
usage, the reserve remains debited; a known provider cost replaces it.

Chat Completions, Responses, and Messages accept text and functions executed
by the client. No images/audio, hosted tools, previous_response_id, or Responses
storage. Unsupported options are rejected. Metadata/cache grants neither identity
nor cache guarantee. Messages works without thinking. Truncated outputs remain
length/incomplete/max_tokens; do not execute a truncated call as a complete function.
Logs: identifiers, tokens/cost, duration; no content nor credentials.

Codex Profile 0.153.4: ATLAS_API_KEY key loaded from the private file in the
client environment, base of the tunnel. Configuration fragment:

```toml
model = "atlas-code"
model_provider = "atlas"
model_reasoning_effort = "none"
model_reasoning_summary = "none"
model_instructions_file = "/chemin/atlas/prompts/dev-client-system.txt"
web_search = "disabled"
[model_providers.atlas]
name = "Atlas"
base_url = "http://127.0.0.1:8030/v1"
env_key = "ATLAS_API_KEY"
wire_api = "responses"
[features]
enable_request_compression = false
multi_agent = false
goals = false
remote_plugin = false
```

The short profile bounds initial instructions; the context of an entire repository
may exceed the budget. Retain the client's usual execution protections.
Source: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-advanced).

Claude Code Profile 2.1.268: ANTHROPIC_BASE_URL=http://127.0.0.1:8030,
ANTHROPIC_API_KEY derived from its own private key, CLAUDE_CODE_DISABLE_THINKING=1,
CLAUDE_CODE_MAX_OUTPUT_TOKENS=512, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, and
CLAUDE_CODE_ATTRIBUTION_HEADER=0. Command `claude --bare --model atlas-code --tools Bash
--allowedTools Bash --system-prompt "$(cat prompts/dev-client-system.txt)" -p "task"`.
This bridge to a non-Claude model is experimental and not supported by the
client provider; no promise of compatibility with future versions.
Source: [gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol).

`make verify-m15` executes HTTP replay without secrets, then, if the Scaleway key is
present, a real function/result round-trip and large context. Each live test key
has a cap of 50,000 micro-EUR. Local proofs in BRAIN/eval/devapi*.json;
CI by PR consumes no inference. Native clients are tested separately
on synthetic folders, with external assertions on the generated file.

Native reproduction: Docker CPU and python:3.12-slim image pinned by digest in
`tests/devapi_clients.py`, Codex binary 0.153.4 (Apache-2.0, official installed package)
and Claude Code 2.1.268 (provider license, official test package) required.
Define ATLAS_CODEX_VENDOR_DIR to the installed client's x86_64 vendor directory and
ATLAS_CLAUDE_BINARY to its native executable, then `make test-devapi-clients`.
Versions verified before inference; no dependencies of these clients in the server.
The harness mounts only binary, synthetic folder, relay, and API socket into
containers without network/capabilities. No cloud/GitHub keys nor personal
configuration. The Codex bypass is limited to this container; the grader is a second
container without keys with a read-only folder. Automatic cleanup on exit.
Each client has 4 requests and 50,000 micro-EUR total; redacted proof in
BRAIN/eval/devapi-clients.json, without real user content.
