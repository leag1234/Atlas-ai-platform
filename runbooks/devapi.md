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

## Atlas assistant keys (design 19 K-A)

The public base URL is `https://atlas.eu.com/api/atlas/v1`. It exposes
`GET /models`, `POST /chat/completions` (JSON and SSE), and `GET /usage`.
`atlas` selects the installation's default chat profile; configured profile IDs
are explicit alternatives. These execute the same assistant pipeline, including
search, citations, document inputs and image inputs. `atlas-code` selects the
existing raw model API and requires `model`; assistant profiles require `assistant`.
Project selection, Work, cancellation tickets, location tickets and connectors
are not granted by either scope in K-A. Cookies and Origin headers are rejected
on this prefix. The browser endpoint retains its Origin checks.

An administrator must issue every key. Self-service is off. Default lifetime is
90 days, maximum 365. Set these private environment variables in both the adapter
and the administrator process: `ATLAS_API_KEYS_DB` (persistent private SQLite),
`ATLAS_API_KEY_PEPPER` (at least 32 random bytes). The administrator process also
requires `ATLAS_API_KEY_ADMIN_TOKEN` (a distinct random secret, at least 32 bytes)
and `ATLAS_API_KEY_ADMIN_SUBJECT` (the operator's audit identity). Provision secrets
with the installation's private environment manager; do not paste values into
shell history. Losing/changing the pepper invalidates all new keys. Back up the
SQLite database and pepper separately; directories must be private, database 0600.

Run administration separately, bound to loopback, never behind a public proxy:

```sh
bash scripts/serve-api-key-admin.sh
python -m services.orchestrator.api_key_admin create \
  --login tester@example.invalid \
  --name external-test --scope assistant --lifetime-days 90 \
  --daily-requests 20 --daily-micro-eur 1000000 \
  --key-file /private/tester.key
python -m services.orchestrator.api_key_admin list
python -m services.orchestrator.api_key_admin revoke --id KEY_ID
```

The CLI calls the loopback HTTP API (`POST/GET /keys`, `DELETE /keys/{id}`). Its
administrator token comes from the environment. Only public metadata is printed;
the credential is written once to an exclusive 0600 file, never stdout. Deliver
that file privately. HTTP creation returns the credential once to the authenticated
administrator; a future UI must handle that response as a secret. Never log it.
Use `--admin-url http://127.0.0.1:PORT` for an isolated installation.

`login` resolves through the authenticated WebUI directory to the exact forwarded
user ID, placing API calls and UI calls in the same daily cost/search ledger. Keep `ATLAS_DATA_DIR` and the normal user/global
budget settings identical for UI, API and legacy developer entry. In project mode,
use real organization/member UUIDs: the identity broker checks current membership
on every request. Stable single-organization mode has no membership service;
revoking all keys for a departed tester is an administrator responsibility.

A key adds its own daily request and micro-EUR ceilings. Reservations are atomic;
unknown/cancelled costs retain their bound. Request counts are not refunded.
Key days reset at UTC midnight; the existing member ledger retains Paris time.
401 means missing/invalid/expired/revoked credentials; 403 names the missing scope;
429 includes used/requested and quota values. Lists contain prefix, required expiry,
creator, last use and truncated IP, never a hash or credential. SQLite audit records
contain creation, revocation and authentication/scope/quota failures; daily summary
rows contain counts and costs only, no request or response content.

Example: read the key into curl's private configuration over stdin, without putting
it in process arguments. Do not enable tracing or verbose HTTP logging.

```sh
python - <<'PY' | curl --silent --show-error --config - \
  https://atlas.eu.com/api/atlas/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data '{"model":"atlas","messages":[{"role":"user","content":"Find current space news and cite sources."}]}'
from pathlib import Path
key = Path('/private/tester.key').read_text().strip()
print('header = "Authorization: Bearer ' + key + '"')
PY
```

For the official Python client, initialize `OpenAI(api_key=Path(...).read_text().strip(),
base_url="https://atlas.eu.com/api/atlas/v1")` and call
`client.chat.completions.create(model="atlas", messages=..., stream=True)` (or
`stream=False`). Atlas keys are independent of Open WebUI keys. Preserve existing interim tester
access until rotation; an owner can then disable Open WebUI key issuance.

### Migration and downloads

Stop the local developer listener before import to avoid concurrent writes. Give
both services the same `ATLAS_DEVAPI_DB`, `ATLAS_API_KEYS_DB` and pepper, then run:

```sh
python -m services.orchestrator.api_key_admin import --id service-test \
  --org-id INSTALLATION_ORG --member-id SERVICE_MEMBER_ID \
  --subject service-test --budget-user SERVICE_BUDGET_ID --name imported-service
```

Import retains the SHA-256 hash, quotas, revocation state and existing reservations,
and marks the key for rotation. Imported keys receive `model` only and a 90-day
migration expiry. Restart the local developer listener with the key-store settings:
its existing credential continues working at the local `/v1` entry, but all future
usage and revocation use the imported store. The old operator revoke also remains
effective at that entry. Do not remove the legacy database until rotation is complete.
The public nginx edge accepts `atl_` keys only: issue a replacement with scope `model`
and revoke the imported key before moving a legacy client to the public prefix.
Reimport is idempotent. Do not run old binaries against an already migrated listener;
that would bypass the new expiry and quotas. Rollback requires stopping that listener.

**K-A download limitation:** an Atlas API key does not authorize generated file,
image or local source-passage download URLs outside `/api/atlas/v1/`. Text answers
and public web citations work; generated artifacts still require the corresponding
browser session/basic authentication. In project mode some legacy artifact routes
are disabled altogether. A key-only client must not treat those links as downloadable
with its Bearer key. API artifact delivery and project-scoped downloads are not part
of K-A; no public download bypass has been added.

### Login resolution and clean API answers (K-A)

In stable mode use `--login tester` or `--login tester@example.invalid` with
`--name external --scope assistant --key-file /secure/delivery.key`; no internal
WebUI identifier is needed. The admin HTTP body likewise accepts `login`, `name`
and `scopes`. Configure `ATLAS_WEBUI_URL` (the private WebUI HTTP listener) and
`ATLAS_WEBUI_ADMIN_TOKEN` (an administrator session token, injected securely).
The administrator service reads `/api/v1/users/` with pagination and resolves the
email to the exact forwarded user ID used by the budget ledger. It does not create
users. A missing login returns `user_never_signed_in` (404): ask the user to sign
in once. An ambiguous local part returns 409: supply the full email. Inactive users
return 403. Directory failures return 503 and never select a guessed budget bucket.
Project mode retains org/member selection and can also resolve `login`.

Both `/api/atlas/v1/chat/completions` and plain `/v1/chat/completions` put only
the final cited answer in `choices[].message.content` / `delta.content`. API SSE
buffers answer text until the assistant finishes; progress remains available during
execution in the extra `atlas.steps` field, and the final frame includes the complete
`atlas.steps` and `atlas.sources` arrays. JSON responses include the same arrays.
Sources are retrieved URLs; inline citations stay in answer content.
Official OpenAI clients accept these extra fields. Browser code explicitly sets
`atlas_ui: true` to retain existing progress rendering; this is a presentation flag,
not authorization. The Atlas key edge always forces it off. No client header is
used to infer the presentation.

Legacy service-key imports retain their explicit service budget identity and do not
require an interactive login. This exception only imports an existing developer
credential as model scope; new stable keys still require a signed-in login.
