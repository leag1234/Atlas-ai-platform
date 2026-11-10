# Atlas API

Atlas is in beta. This guide is for anyone who calls the hosted Atlas API with a key: applications,
scripts, data processing, assistants and coding agents. For the self-hosted developer API and the full
list of supported OpenAI formats, see [API compatibility](api-compatibility.md).

Every key gives the same offer. Keys differ only by their budget and their expiry date; the model name
in each request chooses what Atlas does.

## 1. Quick start

| Item | Value |
|---|---|
| Base URL | `https://atlas.eu.com/api/atlas/v1` |
| Authentication | `Authorization: Bearer <key>` (or `x-api-key: <key>`) |
| Formats | OpenAI Chat Completions (`/chat/completions`), OpenAI Responses (`/responses`), Anthropic Messages (`/messages`), all with streaming |
| Other routes | `GET /models` (the catalogue), `GET /usage` (your spend and limits) |

Keep the key in an environment variable. Never commit it, print it, paste it in a prompt, a ticket or
a log, or share it with another person: each key has its own budget and its own audit trail.

```sh
export ATLAS_API_KEY="$(cat ~/path/to/your.key)"

curl -s https://atlas.eu.com/api/atlas/v1/chat/completions \
  -H "Authorization: Bearer $ATLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"atlas","messages":[{"role":"user","content":"Hello"}]}'
```

Any OpenAI-compatible client works. With the Python SDK:

```python
import os
from openai import OpenAI

client = OpenAI(base_url="https://atlas.eu.com/api/atlas/v1", api_key=os.environ["ATLAS_API_KEY"])
reply = client.chat.completions.create(
    model="atlas-chat",
    messages=[{"role": "user", "content": "Summarize this support ticket in two sentences: ..."}],
)
print(reply.choices[0].message.content)
```

Streaming works as usual (`"stream": true`). Add `"stream_options": {"include_usage": true}` to
receive token usage in the final chunk before `[DONE]`. Responses report the model that answered in
`model`, and for direct models the provider route in `served_by`.

## 2. Models

### The catalogue

| Model | What it is | Use it for | Formats |
|---|---|---|---|
| `atlas` | The Atlas assistant: Atlas instructions and tools around a large model (Qwen 3.5, 397B parameters, by default) | Questions, research with sources, questions on attached documents and images | All three |
| `atlas-qwen`, `atlas-glm`, `atlas-deepseek` | The Atlas assistant with a chosen model: Qwen 3.5 397B, GLM 5.2 or DeepSeek V4 Flash | The same, when you need a given model | All three |
| `atlas-dev` | A direct model, no Atlas layer: GLM 5.3 Flash at effort `none`, GLM 5.3 above; DeepSeek V4 Flash as fallback | Code, coding agents, your own tools (function calling) | All three |
| `atlas-chat` | A direct model, no Atlas layer: the same models as `atlas-dev`; GLM 5.2 as fallback | Volume processing: summaries, translation, classification, extraction | All three |

`atlas-code` (the former name of `atlas-dev`) and the dated aliases listed by `GET /models` remain
accepted; responses to `atlas-dev` still report `atlas-code` in `model`. Both direct models try Mistral first and fall back to Scaleway when it is unavailable; the
assistant runs on Scaleway.

**Assistant or direct model.** The assistant adds its own instructions and tools: it searches the web
and cites what it retrieved, reads attached documents, and answers in the user's language. You do not
control its system prompt. A direct model does exactly what your messages ask, with your own system
prompt and your own tools, and has no web search. Pick the assistant for "answer this well"; pick a
direct model when your application controls the prompt, the format and the cost.

### What each one can do

Measured on the hosted service on 2026-11-02:

| Capability | `atlas` and profiles | `atlas-dev`, `atlas-chat` |
|---|---|---|
| Text | yes | yes |
| Image analysis (`image_url` parts) | yes | yes |
| Attached documents (`file` parts: text, PDF, Word, Excel) | yes | yes, as extracted text |
| Web search, with the retrieved sources cited | yes | yes, run by Atlas on request (section 5) |
| Your own tools (function calling) | no (`tools` is ignored) | yes |
| Image generation | not available on the hosted service yet | no |

### The model name in a request

- **Chat Completions**: `model` is optional. Without it, every key gets `atlas`.
- **Responses and Messages**: `model` is required (400 without it). They serve the assistant and the
  direct models. The assistant runs its own tools: a request that gives it client tools (functions,
  custom, shell…) answers `400 assistant_runs_its_own_tools`; use `atlas-dev` or `atlas-chat` for your
  own tools. The hosted `web_search` tool is accepted, since the assistant searches by itself.
- A name that Atlas does not know is served as `atlas-dev` on the direct formats: `gpt-5-codex`,
  `claude-sonnet-5-5` or a typo all reach `atlas-dev`. Set the name explicitly and check `model` and
  `served_by` in the first responses.

## 3. Reasoning effort and cost (direct models)

### Which reasoning effort

`reasoning_effort` accepts `none`, `low`, `medium` and `high`, in increasing order. `none` means no
reasoning and is the default. Moving from `none` to `low` also changes the model: the cheaper GLM 5.3
Flash becomes the full GLM 5.3, which costs about nine times more per token. `low`, `medium` and `high`
use the same model and differ in how long it reasons.

Measured on 2026-11-02 with `atlas-dev` (15 calls per level: counting, time arithmetic, a coin-change
count, and two Python functions checked by unit tests):

| Effort | Correct | Average charge per call |
|---|---|---|
| `none` | 13 of 15 | 0.000036 EUR |
| `low` | 15 of 15 | 0.000301 EUR |
| `medium` | 15 of 15 | 0.000326 EUR |
| `high` | 15 of 15 | 0.000343 EUR |

The same holds for coding agents. A Codex worker running on Atlas was given two bug-fixing tasks (a
small project with four seeded bugs, and one with ten), three runs per effort level, with the tests
kept read-only:

| Task | Effort | Tasks solved | Average charge | Average tool steps |
|---|---|---|---|---|
| Four bugs | `none` | 3 of 3 | 0.0044 EUR | 5 |
| Four bugs | `low` | 3 of 3 | 0.0187 EUR | 6 |
| Ten bugs | `none` | 3 of 3 | 0.0117 EUR | 7 |
| Ten bugs | `low` | 3 of 3 | 0.0604 EUR | 12 |

Start at `none`; move a task to `low` when `none` does not get there; keep `high` for hard diagnosis.

### What it costs

Prices per million tokens, in euros excluding tax:

| Model | Input | Cached input | Output |
|---|---|---|---|
| GLM 5.3 Flash (direct, `none`) | 0.1275 | 0.0255 | 0.425 |
| GLM 5.3 (direct, `low` and above) | 1.19 | 0.119 | 3.74 |
| DeepSeek V4 Flash (`atlas-dev` fallback, `atlas-deepseek`) | 0.40 | | 0.80 |
| GLM 5.2 (`atlas-chat` fallback, `atlas-glm`) | 1.80 | | 5.50 |
| Qwen 3.5 397B (`atlas`, `atlas-qwen`) | 0.60 | | 3.60 |

The assistant usually costs more than a direct model for the same question: it uses a larger model and
its own instructions and tools add tokens.

Mistral caches the repeated start of a prompt and bills cached tokens at the cached rate; Atlas passes
this through (`usage.prompt_tokens_details.cached_tokens`). Measured with a 10,162-token prompt repeated
five times: GLM 5.3 charged 0.0121 EUR for the first call and 0.0013 EUR (about 89% less) from the
second; GLM 5.3 Flash charged 0.0013 EUR, or 0.0005 EUR when the cache hit, which happened on 2 of 5
calls. Keep the beginning of your prompts identical from one request to the next (system prompt, tool
definitions, earlier turns) and put what changes at the end.

### `max_tokens`

Reasoning tokens count toward `max_tokens`. If the budget is too small, the model spends all of it
thinking and returns no answer; Atlas retries and then answers `503 key_service_unavailable`. With any
effort above `none`, set `max_tokens` to at least 1000 (or leave it out). When `max_tokens` is omitted,
the default is 16384 and the maximum is 32768.

## 4. Images and documents

Send them as content parts of a user message:

```sh
curl -s https://atlas.eu.com/api/atlas/v1/chat/completions \
  -H "Authorization: Bearer $ATLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"atlas","messages":[{"role":"user","content":[
        {"type":"text","text":"What is the budget decided in this document?"},
        {"type":"file","file":{"filename":"minutes.pdf","file_data":"<base64>"}},
        {"type":"image_url","image_url":{"url":"data:image/png;base64,<base64>"}}]}]}'
```

The same works on the other formats with their own names:

| Format | Image | Document |
|---|---|---|
| Chat Completions | `image_url` part | `file` part (`filename`, `file_data` in base64) |
| Responses | `input_image` (`image_url`, optional `detail`) | `input_file` (`filename`, `file_data`) |
| Messages | `image` block (`base64` or `url` source) | `document` block (`text`, `base64` PDF or `content` source); `search_result` blocks are read as text |

The whole request is limited to 8 MiB. A file that cannot be read answers 400 instead of being
ignored. References to stored files (`file_id`, `file_url`) are not available: send the content.
Audio input (`input_audio`) is refused by name, since no hosted model takes audio yet.

## 4 bis. Speech synthesis

When the local speech service is configured, `POST /audio/speech` is available in the OpenAI format.
Any model name is accepted and served by `atlas-tts`. `input` is a non-empty string of at most 12000
characters. `voice` is accepted for client compatibility but ignored: the voice follows the language
and is returned in `X-Atlas-TTS-Voice`. `response_format` is `mp3` (default) or `wav`; `speed` must be
`1.0`; `instructions` are refused by name. The request is charged at 1 micro-euro per character
(CPU-only estimate), so request-count and daily quotas apply. If the local service is unavailable,
the edge answers 503 `tts_unavailable`; strict privacy mode remains supported because synthesis is
local.

## 4 ter. Speech to text

When a transcription provider is configured, `POST /audio/transcriptions` is available in the
OpenAI format and `atlas-transcribe` is listed by `GET /models`. Send `multipart/form-data`:

```sh
curl -s https://atlas.eu.com/api/atlas/v1/audio/transcriptions \
  -H "Authorization: Bearer $ATLAS_API_KEY" \
  -F model=whisper-1 -F language=fr -F file=@meeting.mp3
```

- `file` (required): one audio file of at most 25 MiB (413 `body_limit` with `measured` and
  `limit` above that; the whole form is limited to 25 MiB + 64 KiB).
- `model`: any name, served by `atlas-transcribe`.
- `language` (optional): an ISO-639-1 code such as `fr`.
- `response_format`: `json` (default: `text`, plus `language`, `duration` and
  `usage.seconds` when the provider measures them), `text` (plain text) or `verbose_json`
  (`task`, `language`, `duration`, `text`, `segments` with `start`, `end`, `text`). `srt` and
  `vtt` are refused by name.
- `temperature` (optional): 0 to 1.
- `timestamp_granularities[]`: `segment` only, with `verbose_json`; `word` is refused by name.
- `prompt` is refused by name; any other field is refused as an unsupported field.

The audio is processed in memory by an EU provider and is never stored or logged, nor is the
transcript. The request is charged by audio duration at 4 000 micro-euros per minute (a rounded-up
pass-through of the provider's published price); request-count and daily quotas apply. Errors:
429 `provider_limit` when the provider refuses for quota or rate (never reported as an outage),
502 `provider_error`, 503 `transcription_unavailable`, 400 `invalid_request` when the audio cannot
be decoded. Strict privacy mode answers 503 `strict_privacy_unavailable`: no owned transcription
server exists yet. `POST /audio/translations` answers 400 `translation_not_supported`: transcribe,
then translate the text with a chat model.

## 4 quater. Embeddings

`POST /embeddings` follows the OpenAI format. Use the model `atlas-embed` (Qwen3 Embedding 8B, served by
Scaleway in Europe):

```sh
curl https://atlas.eu.com/api/atlas/v1/embeddings \
  -H "Authorization: Bearer $ATLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "atlas-embed", "input": ["Bonjour", "Hello"]}'
```

- `input`: a non-empty string or a list of up to 2048 non-empty strings, 32768 tokens each, 8 MiB of
  text per request.
- `encoding_format`: `float` (default) or `base64`.
- `dimensions`: optional; vectors have 4096 dimensions by default. A smaller value returns shorter
  vectors, useful for storage (for example in pgvector).
- Cost: input tokens only, at the provider price (0.10 EUR per million tokens, excluding tax), counted
  on the key like any other request.
- Use the same model and the same `dimensions` for indexing and for queries.

## 5. Tool calls

Function definitions, tool-call history, tool results and `tool_choice` (`auto`, `none`, `required` or a
named function) are supported with `atlas-dev` and `atlas-chat` on Chat Completions and Responses. Your
client executes the tools; Atlas never runs them for you.

```sh
curl -s https://atlas.eu.com/api/atlas/v1/chat/completions \
  -H "Authorization: Bearer $ATLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"atlas-dev","tool_choice":"required",
       "tools":[{"type":"function","function":{"name":"get_time","description":"Get the time",
         "parameters":{"type":"object","properties":{"tz":{"type":"string"}},"required":["tz"]}}}],
       "messages":[{"role":"user","content":"What time is it in Paris?"}]}'
```

### Responses tools

On Responses, the tools of the OpenAI format are translated for the direct models:

| Tool | Who runs it | What the client receives |
|---|---|---|
| `function`, `custom`, `namespace` | the client | `function_call` or `custom_tool_call` items |
| `local_shell`, `shell` | the client | `local_shell_call` or `shell_call` items (`action.commands`) |
| `apply_patch` | the client | `apply_patch_call` items (`operation`: `create_file`, `update_file`, `delete_file`, with a V4A diff) |
| `web_search` | **Atlas**, between model turns, up to 4 turns | a `web_search_call` item per search (`action.query`, `action.sources`), then the answer |

The same hosted web search is available on the other formats, with their own names:

| Format | Request | What the client receives |
|---|---|---|
| Chat Completions | `web_search_options: {}` | the answer, with `url_citation` annotations for the retrieved urls it quotes |
| Messages | a `web_search_20250305` (or later) tool named `web_search` | `server_tool_use` and `web_search_tool_result` blocks before the answer, and `usage.server_tool_use.web_search_requests` |

Web search uses the same search chain as the assistant. In strict privacy it is refused with
`web_search_unavailable_in_strict_privacy`. The other hosted tools of the OpenAI format (`file_search`,
`image_generation`, `mcp`, `computer_use_preview`) are not available yet.

### Hosted code execution

Responses accepts `{"type": "code_interpreter", "container": {"type": "auto"}}`; Messages accepts
`{"type": "code_execution_20250825", "name": "code_execution"}` (or the `20250522` version). Atlas runs
Python in a per-call, network-disabled, read-only Docker sandbox with no mounted files and no persistent
container (1 CPU, 1 GB of memory, 64 processes, 256 MB of scratch space in `/tmp`). Each request
allows at most four executions (`max_uses`, default 4), each execution has a 60-second wall-clock limit
and 64 KB of stdout and of stderr, and each API key has a daily execution limit controlled by
`ATLAS_API_CODE_EXECUTION_DAILY_LIMIT` (default 100; over it the call fails with
`code_execution_daily_limit`, shown as `too_many_requests` in the Messages format). Responses returns
`code_interpreter_call` items (a failed call has `status: "failed"` and the reason in its logs);
Messages returns `server_tool_use` and `code_execution_tool_result` blocks. Nothing persists between
executions. Code execution is available in strict privacy, since it runs on Atlas machines. Generated
files and images are not returned in this phase.

## 6. Coding agents

### Codex CLI

Use a dedicated Codex home for Atlas, separate from any other login, for example
`export CODEX_HOME=~/.codex-atlas`, with this `config.toml`:

```toml
model = "atlas-dev"
model_provider = "atlas"
model_reasoning_effort = "none"
model_reasoning_summary = "none"
model_context_window = 256000
model_auto_compact_token_limit = 60000
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

[profiles.heavy]
model_reasoning_effort = "high"
```

`codex exec -p heavy ...` switches one task to the heavy profile. The warning "Model metadata for
atlas-dev not found" is expected. Keep the two context lines: without them Codex never compacts a long
session. Agents resend their whole context at every step, so a lower compaction limit lowers the cost
of long tasks.

### Claude Code

```sh
export ANTHROPIC_BASE_URL=https://atlas.eu.com/api/atlas/v1
export ANTHROPIC_AUTH_TOKEN="$ATLAS_API_KEY"
unset ANTHROPIC_API_KEY
export ANTHROPIC_MODEL=atlas-dev
export ANTHROPIC_DEFAULT_OPUS_MODEL=atlas-dev
export ANTHROPIC_DEFAULT_SONNET_MODEL=atlas-dev
export ANTHROPIC_DEFAULT_HAIKU_MODEL=atlas-dev
export CLAUDE_CODE_SUBAGENT_MODEL=atlas-dev
export CLAUDE_CODE_DISABLE_THINKING=1
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export ENABLE_TOOL_SEARCH=false
claude --model atlas-dev
```

Extended thinking and hosted tools (web search, code execution) are not available through Atlas.

### Running agents cost-effectively

- Bound every task: one clear goal, a time limit and a turn limit. A worker that rereads the same files
  in a loop burns budget without progress: stop it.
- One writer per file or git branch. Two agents never edit the same files.
- Watch `GET /usage` during the first runs.

### What a coding project costs

Figures measured on our own development work so far (November 2026), with Codex CLI agents on
`atlas-dev`:

- A coding agent sends 35,000 to 50,000 tokens of context per step and gets a few hundred tokens back;
  most of that context is served from the provider cache. One step costs about 1 to 2 euro cents. A day
  of work by several agents on one project costs a few tens of euros.
- On a benchmark of 20 small coding tasks (same Codex harness, same hidden checks), the cost per solved
  task compares as follows with the OpenAI API at equal quality:

| Solved | Atlas | OpenAI API | OpenAI cost, compared with Atlas |
|---|---|---|---|
| 19 of 20 | `atlas-dev`, effort `high` | GPT-6.1-sol | about 1.4 times |
| 17 of 20 | `atlas-dev`, effort `low` | GPT-5.3-codex | about 4.7 times |

OpenAI costs are computed from the tokens of each run and OpenAI's public prices. These tasks are short
and well specified: long tasks on large codebases are not measured yet. We keep measuring cost and
quality on different projects and will update these figures.

### OpenAI and Anthropic compatibility

The goal is the full OpenAI and Anthropic APIs. [API coverage](api-coverage.md) lists every route,
parameter, tool and block as applied, accepted without effect, refused or not implemented. A summary of
what is covered on the direct models, as of 2026-11-03:

- **Applied:**
  - Chat Completions: `temperature`, `top_p`, `stop`, `seed`, `frequency_penalty`, `presence_penalty`, `max_tokens` or `max_completion_tokens`, `n` (up to 8 choices), `logprobs` and `top_logprobs`, `response_format`, tools and `tool_choice`, `parallel_tool_calls`, `reasoning_effort`, `stream` and `stream_options`;
    - `response_format: {"type":"json_schema","json_schema":{"name":...,"schema":{...},"strict":true}}` is guaranteed on every public model (`atlas-dev`/`atlas-code`, `atlas-chat` and the dated aliases): each route targets only providers with native structured output, and Atlas validates the final JSON against the schema with one retry. If the answer still does not validate, a non-streamed answer is HTTP 502 `invalid_response_format` and a stream ends with an error event, never the invalid JSON as a success. A route that cannot guarantee the schema refuses the request with HTTP 400 `unsupported_parameter` (`param: "response_format"`) before any provider call;
  - Responses: `top_p`;
  - Messages: `stop_sequences`, `top_p`, `top_k`.

  Legacy `functions` and `function_call` map to tools, and `developer` messages to system messages.
- **Accepted without effect:** `store`, `metadata`, `user`, `safety_identifier`, `service_tier`, `verbosity`, `prompt_cache_key`, `prompt_cache_retention`, and `cache_control` markers. Atlas caches the repeated start of prompts by itself, and responses report `store: false`.
- **Refused by name:**
  - `logit_bias`: no hosted provider applies it;
  - `audio`, `modalities`, `prediction`;
  - any unknown field: the error names it, for example `unknown field messages.2.foo`.
- **Other routes:** `GET /models` and `GET /models/{id}` (in the Anthropic format for Anthropic clients), `POST /responses/input_tokens` and `POST /messages/count_tokens` (estimated tokens).

**Nothing is stored.** Atlas keeps no response or conversation. Send the full conversation history with
each request. These features answer `storage_not_supported` with that instruction:
- `previous_response_id`, `conversation` and `background: true` answer 400;
- retrieving, deleting or cancelling a response, its input items, the `/conversations` routes, and stored Chat Completions answer 404.

## 7. Budget, limits and usage

Every key has its own budget, set when it was issued (a weekly or daily amount in euros), and an expiry
date. Check where you stand at any time:

```sh
curl -s -H "Authorization: Bearer $ATLAS_API_KEY" https://atlas.eu.com/api/atlas/v1/usage
```

The reply gives the day and week counters (`requests`, `charged_micro_eur`; one million micro-euros
is one euro) and the limits that apply to your key.

| Limit | Value |
|---|---|
| Rate | 600 requests per minute per key (burst 200), and 1800 per minute per client address (burst 400). Everyone behind one office or VPN address shares the second limit: start parallel workers a few seconds apart. |
| Request size | up to 8 MiB, up to 4096 messages of history |
| Output | 16384 tokens by default, at most 32768 |

## 8. Errors

| Status | Meaning | What to do |
|---|---|---|
| 400 `invalid_request` or `body_limit` | A field is invalid (for example an unknown `reasoning_effort`), the body is larger than 8 MiB, or the history has more than 4096 messages | Fix or shorten the request |
| 400 `assistant_runs_its_own_tools` | Client tools were given to `atlas` or a profile | Use `atlas-dev` or `atlas-chat` for your own tools |
| 400 `file_data_required`, `invalid_file_data`, `unreadable_file` | An attached file is missing, not base64, or has no readable text | Check the file part |
| 400 `unknown field …`, `unsupported_parameter: …`, `unsupported_content: input_audio` | A field Atlas does not know, or an option no hosted model applies; the message names it | Remove it (see "OpenAI and Anthropic compatibility") |
| 400 `storage_not_supported` | `previous_response_id`, `conversation` or `background`: Atlas stores nothing | Send the full history in `input` |
| 400 `web_search_unavailable_in_strict_privacy` | The hosted web search was requested in strict privacy mode | Remove the tool or the strict header |
| 401 | Missing, wrong, expired or revoked key | Check the key and its expiry date |
| 404 `storage_not_supported` | A stored response, conversation or completion was requested | Atlas stores nothing; keep your own history |
| 409 `tool_loop_detected` | The same tool call returned the same result 12 times in a row | Change the approach; do not retry the same call |
| 429 with a JSON body | The budget of the key is reached; the message says whether day or week | Stop, or ask for a higher budget |
| 429 with an HTML page | Rate limit | Wait 30 to 60 seconds and resume; do not restart all workers at once |
| 502 or 503 | The service is restarting for a deployment (usually under a minute) | Wait and retry |
| 503 `strict_privacy_unavailable` | You asked for strict privacy and no owned inference server exists yet | Remove the `X-Atlas-Privacy: strict` header |
| 503 `key_service_unavailable` | A downstream failure; a common cause is a `max_tokens` too small for the reasoning effort | See section 3, then retry |

To discourage loops, Atlas also appends a note asking the model to change approach when the same tool
call returns the same result several times, before it stops the exchange at the twelfth.

## 9. Privacy

Requests are served by open-weight models hosted by European providers (Mistral and Scaleway). A strict
privacy mode (`X-Atlas-Privacy: strict`), which would only use inference servers fully controlled by the
owner, exists but is not available until such a server is configured: until then it answers 503 and
never falls back to a shared provider. Treat the service as a beta and do not send data you could not
share with a hosting provider.

### How strict privacy is configured (operator)

Strict privacy has no settings screen. Which routes accept it is part of the deployment: targets
marked `"privacy": "owned"` in `services/model-gateway/api-routing.json`, each pointing to an
OpenAI-compatible vLLM server that the owner fully controls, whose address is set with
`ATLAS_OWNED_VLLM_BASE_URL` (and `ATLAS_OWNED_VLLM_API_KEY` when the server needs one). A request opts in
with the `X-Atlas-Privacy: strict` header; an operator can also make a key strict when issuing it, and a
project can be set to strict through the project API. In strict mode web search and web fetch are not
offered. See the strict privacy section of [the platform API notes](08-plateforme-api.md).
