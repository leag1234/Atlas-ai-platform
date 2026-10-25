# OpenAI API compatibility

Atlas implements selected OpenAI request/response formats. Use the
[OpenAI API reference](https://platform.openai.com/docs/api-reference) for the
upstream API definitions; the limits below describe this repository.

There are two separate interfaces:

- **Developer API:** Docker `./atlas setup --api`, loopback port 3001, base URL
  `http://127.0.0.1:3001/v1`; standalone `make serve-devapi` uses port 8030. Individual keys, text/functions and model `atlas-code`.
- **Chat adapter:** started by `make serve`, with port offset configuration. At
  offset 2100 its base URL is `http://127.0.0.1:10120/v1`. It powers the web assistant,
  including Atlas-specific document, project and image flows. It has no developer
  key/quotas layer; keep it private behind the intended UI/front door.

The developer API has no access to the web assistant's conversations, retrieval,
MCP tools or files. Speech services use separate local endpoints.

## Support by theme

“Supported” refers to the behavior described, not every upstream option.

| Theme | Status | Atlas behavior |
| --- | --- | --- |
| Chat completions | Partial | Both expose `POST /v1/chat/completions`. Developer requests accept text, system/developer/user/assistant/tool messages, one choice (`n` omitted or `n=1`) and selected generation options. Unknown developer fields are rejected; the chat adapter has a different schema. |
| Streaming | Supported | Chat Completions uses SSE text/tool deltas and `[DONE]`. Developer clients can request usage with `stream_options.include_usage`: ordinary chunks carry `usage: null`, followed by a chunk with empty `choices` and total usage. Handle error frames after streaming starts. |
| Models list | Supported | `GET /v1/models`: developer API lists `atlas-code`; chat lists `atlas-qwen`, `atlas-glm`, `atlas-deepseek`. These are routing aliases, not a provider catalog. |
| Tool calls | Partial | Developer API accepts function definitions, tool-call history, client tool results and `auto`/`none`/`required`/named selection; supports parallel-call control. Clients execute functions. Hosted OpenAI tools are absent; the chat assistant runs its own configured tools. |
| Images | Partial | Chat accepts image content and provides Atlas-specific image upload/display/generation flows. Developer input is text only. No OpenAI `/v1/images/generations`, edits or variations endpoint. |
| Files | Not supported | No OpenAI `/v1/files` lifecycle. The web assistant reads attachments and creates downloads through its own document/file services. |
| Embeddings | Not supported | No public OpenAI `/v1/embeddings` endpoint. Retrieval uses an internal CPU gateway with a different `/embeddings` contract. |
| Audio | Partial | Optional services expose `/v1/audio/speech` (Piper, WAV/MP3) and `/v1/audio/transcriptions` (local Whisper, JSON/text). These are separate from the developer API. No Realtime API. |
| Authentication and keys | Supported | Developer API accepts `Authorization: Bearer` or `x-api-key`; if both are present they must match. Keys can be revoked, are stored as hashes and have individual daily quotas. |
| Limits and errors | Partial | Atlas enforces its own request, context, cost and time limits. Developer errors, including unsupported routes, use `error.type` and `error.message`; error codes and headers are not a complete OpenAI match. Daily quota errors do not include `Retry-After`. |
| Structured JSON output | Partial | Developer Chat `response_format` and Responses `text.format` accept `text`, `json_object` and `json_schema`. JSON output is validated server-side; supported schema subset, retries and stream failure behavior are described below. |
| Responses | Partial | `POST /v1/responses` supports text/functions, non-streamed JSON response envelopes and streaming. No stored responses, `previous_response_id`, hosted tools or multimodal input. |

## Start the developer API

For Docker, run `./atlas setup --api` (or `./atlas setup --reconfigure --api`),
then `./atlas api-key create editor --daily-requests 20`. Creation prints a private
mode-600 file path under `BRAIN/api-keys/`; only explicit `--show` reveals the key.
Use `./atlas api-key list` and `./atlas api-key revoke editor` to manage keys.
No key is created automatically. Use the file as your SDK API key, base URL
`http://127.0.0.1:3001/v1`, and model `atlas-code`. `ATLAS_API_PORT` changes the
published port; `ATLAS_API_BIND=0.0.0.0` opts into all interfaces (supply your own
TLS proxy). The `api` profile and `devapi` volume persist configuration and quotas.
Setup checks the model-list authentication boundary; authenticated SDK calls get
HTTP 200 and the public alias. See [quickstart](quickstart.md).

Standalone legacy startup:

Install the [runtime](installation.md). Supply `SCW_GENERATIVE_BASE_URL` and
`SCW_GENERATIVE_API_KEY` in the process environment or a private local `.env`.
`CODE_MODEL` optionally selects a supported code-role model from the gateway.
Unlike `make serve`, `scripts/serve-devapi.sh` loads only `.env`, not
`ATLAS_SECRETS_FILE`; export your private environment first if needed.

Set `ATLAS_DEVAPI_DB` to a private persistent database path, using the same path
for key administration and serving. Provision a key without printing it:

```bash
python -m services.orchestrator.dev_auth create alice \
  --key-file /secure/path/alice.key \
  --daily-requests 20 --daily-micro-eur 50000
make serve-devapi
```

The key file must not already exist; it is created with mode 600. Deliver it
privately and configure the client to read it without logging its contents.
Tunnel port 8030 for remote clients and use model `atlas-code`. To select a free
loopback port, export the same provider settings and database path, then run
`PYTHONPATH=.:services/model-gateway python -m uvicorn services.orchestrator.devapi:app --host 127.0.0.1 --port <free-port> --no-access-log`;
`make serve-devapi` currently fixes the port at 8030. A minimal request
body for `/v1/chat/completions` is:

```json
{
  "model": "atlas-code",
  "messages": [{"role": "user", "content": "Explain this function."}],
  "max_tokens": 512,
  "stream": false
}
```

Authenticate every request, including `/v1/models` and Atlas-specific `/v1/usage`.
The latter reports only the current key's daily counters. Revoke a key locally:

```bash
python -m services.orchestrator.dev_auth revoke alice
```

## Developer structured output

Chat Completions accepts `response_format: {"type":"text"}` (ordinary text),
`{"type":"json_object"}` (a JSON object), or a named schema:

```json
{"type":"json_schema","json_schema":{"name":"value","schema":{"type":"object","properties":{"value":{"type":"integer"}},"required":["value"],"additionalProperties":false},"strict":true}}
```

For Responses, use `text: {"format": ...}`. The `json_schema` format is flat:
`{"type":"json_schema","name":"value","schema":{...},"strict":true}`;
do not nest another `json_schema` object. Both APIs require a name (1–64 ASCII
letters, digits, underscores or dashes) and an object-valued schema. `strict`
accepts a Boolean and defaults to false. Both strict modes are independently
validated against the supplied schema. Descriptions are optional.

Atlas enforces the JSON-mode context rule: `json_object` requires `json`
(case-insensitive substring) in at least one normalized message's text, including
system/developer instructions. Tool definitions do not count. Explicit `text`
does not require JSON or trigger schema validation. See the
[OpenAI structured-output guide](https://developers.openai.com/api/docs/guides/structured-outputs).

The repository has no `jsonschema` dependency. Its supported subset is: primitive
JSON types (including nullable type lists), `properties`, `required`, Boolean or
schema-valued `additionalProperties`, `items`, `anyOf`, scalar `enum`/`const`,
`minimum`/`maximum`, `minLength`/`maxLength`, `minItems`/`maxItems`, and nonrecursive
local `#/$defs/<name>` references. `title`, `description` and `default` are
annotations. Unsupported keywords (including `pattern`, `format`, `allOf`,
`oneOf`, `multipleOf`, external and recursive references) return 400 before
provider I/O. Schema checking is bounded to depth 16 and 512 visited schema nodes;
output validation to depth 32 and 10,000 visited value/schema pairs. This is a
subset, not full JSON Schema or every OpenAI structured-output restriction.

The legacy Scaleway code route was live-probed on 2026-10-24 with a named schema;
it returned the required object. The requested format is forwarded unchanged
apart from defaults, and Atlas independently parses and validates the final text.
Markdown fences, malformed/nonfinite JSON, and schema mismatches never count as
successful output. A non-streamed failure gets one corrective attempt containing
the bounded validation error, within the original deadline/reservation. If that
also fails, HTTP 502 carries `error.type="invalid_response_format"` and a bounded
validation diagnostic. Provider transport failures are not retried by this feature.

Streams emit deltas as they arrive but defer terminal success and usage until
validation. Once content has been emitted, it cannot be replaced: an invalid
answer ends with an error frame, without retry or a successful finish/completed
event. Chat emits `{"error":{"type":"invalid_response_format","message":...}}`
and one `[DONE]`; Responses emits `type="error"`, `code="invalid_response_format"`
and no `response.completed`. Clients must treat accumulated text as provisional.
An empty stream can retry once before emitting content. `tool_choice="none"`
omits tool definitions and selection options upstream, checks for nonempty text,
and uses the same bounded recovery instead of returning a null successful answer.

When `ATLAS_PROVIDER` is set, the developer API uses the same provider settings
as chat: endpoint/key, text model, per-role context, request options, typed content
and cumulative stream usage. Hetzner system messages are merged at the start;
reasoning options follow the preset. A configured text fallback is tried once on
transient transport errors before any output, within the original deadline.
A partial stream is never replayed. Structured-output correction still gets at
most two attempts. With `ATLAS_PROVIDER` unset, the legacy Scaleway route retains
its existing request shape, settings and pricing.

Unpriced presets (including USD prices without an explicit conversion) log the
existing warning and disable monetary ceilings, including the daily money quota.
Token, context, time and daily request quotas remain active. A route including an
unpriced fallback is also reported as unpriced. Numeric cost telemetry is zero
for such requests; the `unpriced` log field distinguishes this from verified-free
inference. Provider billing still applies. Known-price transient failures retain
the primary reservation when fallback succeeds. Context is checked for both models.

## Developer limits and error handling

Requests have a 1 MiB body limit, up to 1,024 messages and 64 function definitions.
Output defaults to 512 tokens, with a maximum of 8,192; `max_completion_tokens`
is accepted as an alias for `max_tokens`. Only `none` and `low` reasoning effort
are accepted. Input is conservatively budgeted from UTF-8 bytes; the EUR 0.05
per-request ceiling can reject a request smaller than the model context window.
There is a 120-second request deadline. JSON formats and `tool_choice="none"`
reserve up to two attempts within the same EUR 0.05 ceiling and daily reservation.
Other legacy requests retain one attempt; selected providers may use the
configured transient fallback as described above. Both attempts count toward reported tokens and cost;
an unknown attempt retains the full reservation. Larger JSON requests can therefore
reach the cost limit sooner than ordinary text requests.

| HTTP status | Meaning |
| --- | --- |
| 400 | Invalid/unsupported input, body/context limit or cost reservation too large. |
| 401 | Missing, invalid, conflicting or revoked credentials. |
| 404 | Unsupported endpoint; JSON `error.type` and `error.message`, including embeddings, files and image generation. |
| 429 | Developer daily request or monetary quota exhausted; no `Retry-After` header. |
| 502 | Provider failure or invalid final JSON/schema output in a non-streamed inference response. |
| 503 | Request setup unavailable, including missing configuration or setup timeout. |

For a streamed failure, inspect the error event even if HTTP headers already say
200. The official Python and Node clients raise `APIError` for Chat Completions
stream errors. In the tested clients (Python 2.54.0, Node 7.43.0), a Responses
stream error is yielded as `ResponseErrorEvent` by Python and raised as `APIError`
by Node. Applications must handle both their SDK exceptions and Responses error
events; a stream ending without a completed response is not success. A length-limited answer or tool call is incomplete; do not execute an
unfinished function. Quotas reset by UTC day and persist in SQLite. If usage is
missing or a request is cancelled, the reservation remains charged rather than
being silently refunded. Preserve the database across restarts.

The chat adapter has separate limits: ordinary requests use EUR 0.10/120 seconds;
attachments and file-producing requests use EUR 0.30/300 seconds. Its extensions
and validation behavior are defined in
[chat_schema.py](../services/orchestrator/chat_schema.py).

Source: [developer routes](../services/orchestrator/devapi.py),
[request schema](../packages/dev_request.py),
[chat normalization/streaming](../services/orchestrator/dev_chat.py),
[Responses translation](../services/orchestrator/dev_input.py),
[inference/errors](../services/orchestrator/dev_inference.py),
[keys and quotas](../services/orchestrator/dev_auth.py),
[TTS](../services/tts/service.py), [STT](../services/stt/service.py) and
[chat routes](../services/orchestrator/chat_api.py).
