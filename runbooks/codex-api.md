# Codex with an Atlas API key

Use an Atlas key with `model` scope, issued through the Atlas key manager.
The public provider base URL is `https://atlas.eu.com/api/atlas/v1`; Codex sends
`POST https://atlas.eu.com/api/atlas/v1/responses`. Authentication is
`Authorization: Bearer` using the Atlas key. The public endpoint uses the key
manager's expiry, revocation, daily request and money limits, plus its owner's
shared user budget. It does not use a local developer-service key.

Load the key into `ATLAS_API_KEY` through your environment manager or private
key file. For a POSIX shell, without displaying the value:

```sh
export ATLAS_API_KEY="$(cat /private/atlas-coding.key)"
```

Place this provider configuration in `~/.codex/config.toml` (Codex 0.153):

```toml
model = "atlas-code"
model_provider = "atlas"
model_reasoning_effort = "none"
model_reasoning_summary = "none"
web_search = "disabled"

[model_providers.atlas]
name = "Atlas"
base_url = "https://atlas.eu.com/api/atlas/v1"
env_key = "ATLAS_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false

[features]
enable_request_compression = false
multi_agent = false
goals = false
remote_plugin = false
```

Run `codex` from the intended project folder and keep normal sandbox and approval
settings. Use a small initial task: the adapter retains a 1 MiB request-body limit,
120 second inference deadline, and 0.05 EUR maximum reservation per call. Large
repository instructions can exceed the conservative input/output cost bound;
reduce supplied context when the API returns `cost_budget` or `context_exceeded`.
The supervisor checks the real client after deployment; local protocol tests do
not establish live Codex acceptance.

Source: [official Codex provider configuration](https://learn.chatgpt.com/docs/config-file/config-advanced)
and the existing pinned client profile in `tests/devapi_clients.py`.

## Models and supported protocol

`atlas-code` always selects the configured code-role model as a direct raw call.
Explicit `raw/<provider-model-id>` entries from `GET /models` select that configured
model. Unknown client model names, including names beginning with `gpt-`, map to
`ATLAS_API_RESPONSES_MODEL`, a server environment setting accepting a listed raw
ID or `atlas-code` (the default). The returned `model` identifies the selected raw
model. An invalid server default returns 503 when needed; a configured model
without a price returns 400 before inference. Client aliases never create an
unconfigured or unmetered provider route. Chat Completions model selection is
unchanged.

Both JSON and SSE support text and client-executed function tools. SSE includes
`response.created`, item/content lifecycle events, incremental text or function
arguments, and `response.completed` (or `response.incomplete` for output limits).
Return the preceding output items and matching `function_call_output` items in
the next request's `input`; their `call_id` links the tool results. Reasoning
history items are tolerated but not forwarded as text or opaque provider state.
No server-side response storage, `previous_response_id`, hosted tools, images,
compressed requests, or WebSocket transport is supported on this adapter.
Use `store = false` in API requests. Disabling reasoning summaries and hosted web
search in the client avoids unsupported request options.

Failures use an OpenAI `error` object: 401 for absent, invalid, expired or revoked
keys; 403 for missing model scope; 429 for key or user budget limits with measured
values. A failed or interrupted provider call keeps its cost reservation if usage
is unknown. Completed usage is charged once per API request. Provider failures
after SSE starts are terminal `error` events, never successful completions.

The stable nginx `/api/atlas/v1/` prefix already forwards `/responses` with
buffering and proxy caching disabled. No new proxy location is required.
