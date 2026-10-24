# OpenAI API compatibility

Atlas implements selected OpenAI request/response formats. Use the
[OpenAI API reference](https://platform.openai.com/docs/api-reference) for the
upstream API definitions; the limits below describe this repository.

There are two separate interfaces:

- **Developer API:** `make serve-devapi`, loopback port 8030, base URL
  `http://127.0.0.1:8030/v1`. Individual keys, text/functions and model `atlas-code`.
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
| Structured JSON output | Not supported | Chat `response_format` (`json_object` or `json_schema`) and Responses `text.format` are rejected with HTTP 400. A JSON response envelope does not imply JSON-mode or schema-constrained model output. |
| Responses | Partial | `POST /v1/responses` supports text/functions, non-streamed JSON response envelopes and streaming. No stored responses, `previous_response_id`, hosted tools or multimodal input. |

## Start the developer API

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

## Developer limits and error handling

Requests have a 1 MiB body limit, up to 1,024 messages and 64 function definitions.
Output defaults to 512 tokens, with a maximum of 8,192; `max_completion_tokens`
is accepted as an alias for `max_tokens`. Only `none` and `low` reasoning effort
are accepted. Input is conservatively budgeted from UTF-8 bytes; the EUR 0.05
per-request ceiling can reject a request smaller than the model context window.
There is one provider attempt and a 120-second request deadline.

| HTTP status | Meaning |
| --- | --- |
| 400 | Invalid/unsupported input, body/context limit or cost reservation too large. |
| 401 | Missing, invalid, conflicting or revoked credentials. |
| 404 | Unsupported endpoint; JSON `error.type` and `error.message`, including embeddings, files and image generation. |
| 429 | Developer daily request or monetary quota exhausted; no `Retry-After` header. |
| 502 | Provider failure in a non-streamed inference response. |
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
