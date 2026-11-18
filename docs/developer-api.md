# Atlas API

Atlas is in beta. This guide is for anyone who calls the hosted Atlas API with a key: applications,
scripts, data processing, assistants and coding agents. For the self-hosted developer API and the full
list of supported OpenAI formats, see [API compatibility](api-compatibility.md).

Every key gives the same offer. Keys differ only by their budget and their expiry date; the model name
in each request chooses what Atlas does (a key restricted on purpose is the only exception: see
"Keys and scopes" in section 8).

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
| `atlas-code-fast` | A direct model, no Atlas layer: always the effort `none` chain of `atlas-dev` (GLM 5.3 Flash, DeepSeek V4 Flash as fallback). `reasoning_effort`, `reasoning.effort`, `thinking` and `output_config.effort` are accepted and ignored | Low-cost coding agents, including clients that always send an effort level (Claude Code) | All three |
| `atlas-chat` | A direct model, no Atlas layer: the same models as `atlas-dev`; GLM 5.2 as fallback | Volume processing: summaries, translation, classification, extraction | All three |

`atlas-code` (the former name of `atlas-dev`) and the dated aliases listed by `GET /models` remain
accepted; responses to `atlas-dev` still report `atlas-code` in `model`. Both direct models try Mistral first and fall back to Scaleway when it is unavailable; the
assistant runs on Scaleway.

`GET /models` also lists one precise id per routing target, for example `mistral/zai-glm-5-3`,
`mistral/mistral-large-4` or `scaleway/glm-5.2`, with `owned_by` set to the provider. Calling such an
id uses that target only: no fallback to another model. Send `"fallback": false` on an alias request
(`atlas-chat`, `atlas-code`…) to use the first target only. In both cases, when the provider fails,
the error carries its cause — 429 `rate_limit` (with `Retry-After` when the provider sent one), 402
`provider_quota`, 503 `provider_unavailable`, 504 `provider_timeout` — as a JSON error with `type`,
`message` and `provider_status`; the answer never comes from another model. An exhausted provider
account is always reported as 402 `provider_quota`, never as a rate limit or an outage, whatever
status the provider used to say so; `provider_status` carries that status. `served_by` (the
`X-Atlas-Served-By` header and the response field) names the target that answered on every response,
pinned or not, and pricing and the ledger use the pinned target's price.

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
| Your own tools (function calling) | no: ignored on Chat Completions, refused on Responses and Messages | yes |
| Image generation | not available on the hosted service yet | no |

### The model name in a request

- **Chat Completions**: `model` is optional. Without it, every key gets `atlas`. `tools` given to
  the assistant (`atlas` or a profile) are ignored: it answers with its own tools (measured on
  2026-11-18).
- **Responses and Messages**: `model` is required (400 without it). They serve the assistant and the
  direct models. The assistant runs its own tools: a request that gives it client tools (functions,
  custom, shell…) answers `400 assistant_runs_its_own_tools`; use `atlas-dev` or `atlas-chat` for your
  own tools. The hosted `web_search` tool is accepted, since the assistant searches by itself.
- The model you name answers, and the response names it (`atlas`, a profile or a direct model, as
  `GET /models` lists them). Atlas never moves a request to another model because of what it
  contains, and a pasted document never changes how a request is classified.
- A request in a category that Atlas never writes receives a short refusal as the answer, from no
  model, on every route and every model. The hard limits read your own words in the last user message
  (not the system prompt, earlier turns, tool results or a long pasted document). The refusal is a
  normal answer that costs nothing: `finish_reason: "content_filter"` on Chat Completions,
  `status: "incomplete"` with `incomplete_details.reason: "content_filter"` on Responses,
  `stop_reason: "refusal"` on Messages.
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

#### Effort on Messages (`/v1/messages`)

Messages has no `reasoning_effort` field. Atlas reads the effort from the Messages fields, in this order:

| Request | Effort | Model on `atlas-code` / `atlas-dev` |
|---|---|---|
| `output_config.effort` set (`low`, `medium` or `high`) | that value, whatever `thinking` says | GLM 5.3 |
| `thinking: {"type": "enabled", "budget_tokens": N}` | `low` up to 4096, `medium` up to 16384, `high` above | GLM 5.3 |
| `thinking: {"type": "adaptive"}` | `medium` | GLM 5.3 |
| no `thinking` (or `disabled`) and no `output_config.effort` | `none` | GLM 5.3 Flash |

`atlas-code-fast` ignores these fields: every request runs on GLM 5.3 Flash, as effort `none`.

`output_config.effort` values `xhigh` and `max` are refused (HTTP 400). Claude Code 2.1.312 (measured
2026-11-17) sends `output_config.effort` on every request, `high` by default, also when thinking is
disabled (`MAX_THINKING_TOKENS=0` or `CLAUDE_CODE_DISABLE_THINKING=1`): its requests therefore run on
GLM 5.3, never on GLM 5.3 Flash. `CLAUDE_CODE_EFFORT_LEVEL=low` (or `/effort low`) keeps GLM 5.3 with a
shorter reasoning. Set the model to `atlas-code-fast` to run Claude Code on GLM 5.3 Flash. On the code benchmark of 2026-11-05 (20 tasks, Codex CLI), effort `none` (GLM 5.3
Flash) passed 15 tasks for 0.111 EUR and effort `high` (GLM 5.3) passed 19 for 0.819 EUR.

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

Mistral caches the repeated start of a prompt and bills cached tokens at the cached rate; Atlas bills
the same rate and reports the cached count in each format: `usage.prompt_tokens_details.cached_tokens`
on Chat Completions, `usage.input_tokens_details.cached_tokens` on Responses, and
`usage.cache_read_input_tokens` on Messages. On Messages, as with Anthropic, `input_tokens` counts only
the prompt tokens not read from the cache (the whole prompt is `input_tokens + cache_read_input_tokens`),
and `cache_creation_input_tokens` is always 0: writing to the cache costs nothing extra. Targets without
a cached rate in the price table above bill every prompt token at the input rate. Measured with a 10,162-token prompt repeated
five times: GLM 5.3 charged 0.0121 EUR for the first call and 0.0013 EUR (about 89% less) from the
second; GLM 5.3 Flash charged 0.0013 EUR, or 0.0005 EUR when the cache hit, which happened on 2 of 5
calls. Keep the beginning of your prompts identical from one request to the next (system prompt, tool
definitions, earlier turns) and put what changes at the end.

#### Getting cache hits with coding agents

The provider caches a prompt by its start: a request is served from the cache up to the first byte that
differs from an earlier request. Atlas keeps that start stable:

- Earlier turns are converted the same way on every request, and reasoning blocks of earlier turns are
  never sent, so an agent that appends turns sends a body whose start does not change.
- `cache_control` markers are accepted and dropped: moving them between turns changes nothing.
- A system block that holds only a client header line (for example a billing or attribution line
  starting with `x-` and ending in `-header:`) is not sent to the model. Some clients put a value that
  changes on every request there; at the very start of the prompt it would defeat the cache for the
  whole request.
- Atlas sends one cache key per conversation, derived from your API key and the opening messages (system
  prompt and first user message). Measured on 2026-11-17 with 12 agent sessions in parallel on one API key
  (16 turns each, about 20,000 prompt tokens): 81% of the prompt tokens came from the cache with one key
  per conversation, 71% with one key per system prompt.

What your client should do:

- Never put a date, time, request id, counter or random value in the system prompt, the tool
  definitions or the first message. Put such values in the newest message.
- Do not change the tool list during a session (adding or removing an MCP server, switching tool
  search on or off). Each change makes the next request start differently.
- Keep one long session instead of restarting it: the first request of a session is never cached.
- Rewrite earlier turns only when you must (context compaction); the next request then starts a new
  cache.
- Read `cache_read_input_tokens` (Messages) or `cached_tokens` (Chat Completions, Responses) to check.

Measured on 2026-11-17, replaying a 16-turn Claude Code session (2.1.312, 23 tools, about 20,000 prompt
tokens per turn) through `/v1/messages` on GLM 5.3: 15 of 15 later turns hit the cache, 93% of the
prompt tokens were cached, 0.0041 EUR per turn. On GLM 5.3 Flash (effort `none`), hits are less
regular: on the same replay, 10 to 13 of 15 later turns hit and 57 to 78% of the prompt tokens were
cached, 0.0010 to 0.0018 EUR per turn.

### `max_tokens`

Reasoning tokens count toward `max_tokens`. If the budget is too small, the model spends all of it
thinking and returns no answer: the response is a normal length outcome (`finish_reason: "length"`,
`status: "incomplete"` on Responses, `stop_reason: "max_tokens"` on Messages) with an empty content and
the usage you pay for. Atlas does not send the same request again, since it would run out the same way,
and never reports it as a provider failure. An answer cut by `max_tokens` is likewise returned as
written, also with `response_format`. With any effort above `none`, set `max_tokens` to at least 1000
(or leave it out). When `max_tokens` is omitted,
the default is 16384 and the maximum is 32768.

On the assistant (`atlas` and its profiles), `max_tokens` or `max_completion_tokens` (Chat
Completions), `max_output_tokens` (Responses) and `max_tokens` (Messages) cap its final answer,
estimated at 4 bytes of UTF-8 text per token. A longer answer is cut there and ends with the same
length outcome (`finish_reason: "length"`, `status: "incomplete"`, `stop_reason: "max_tokens"`). The
assistant's internal calls (routing, search, tools) keep their own output budget, so a small limit
never breaks a search; `usage` counts all of those calls, which is what you pay, and can therefore
exceed the limit. Without a limit the assistant answers within its own budget (about 6000 tokens).

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
be decoded. Strict privacy mode answers 403 `strict_privacy_unavailable` and Enhanced 403
`privacy_feature_refused`: no owned transcription server exists yet. `POST /audio/translations` answers 400 `translation_not_supported`: transcribe,
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

**Valid arguments, always.** The arguments of every tool call Atlas returns are one valid JSON object:
`function.arguments` on Chat Completions, `arguments` of `function_call` items on Responses, `input`
of `tool_use` blocks on Messages. Each call is checked once it is complete, before your client sees it
(a streamed call therefore arrives in one piece, just before its `finish_reason`; while a long call is
being written, an empty delta keeps the stream alive every 15 seconds):

- safe slips with one reading are rewritten: a code fence around the JSON, a trailing comma, a raw
  line break inside a string, single-quoted (Python-style) keys and strings, arguments encoded twice
  as a JSON string, and empty arguments (`{}`);
- any other invalid JSON, or a call to one of your declared tools without one of its `required`
  parameters, is asked again once, in the same request and within its reservation, with a short
  correction; you pay for both attempts (the usage adds them up). Text already streamed stays as it is;
- if the corrected attempt still returns invalid JSON, the request fails with 502
  `invalid_tool_arguments` (an error event on a stream), never with the invalid arguments. A call that
  is valid JSON but still lacks a required parameter is returned, so that your client can tell the
  model. Send the request again.

An answer cut by `max_tokens` (`finish_reason: "length"`, `status: "incomplete"`, `stop_reason:
"max_tokens"`) is the one exception: its last call is incomplete by nature and is returned as the
model wrote it. Raise `max_tokens` (a whole file in one call needs several thousand tokens).

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
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export ENABLE_TOOL_SEARCH=false
claude --model atlas-dev
```

Extended thinking and hosted tools (web search, code execution) are not available through Atlas.
`CLAUDE_CODE_ATTRIBUTION_HEADER=0` removes the client's billing line from the system prompt (Atlas also
drops it), and `ENABLE_TOOL_SEARCH=false` keeps the tool list fixed during a session: both keep the
start of each request identical, which the prompt cache needs. Claude Code still sends an effort level
on every request (see "Effort on Messages"), so it runs on GLM 5.3 at the level you choose with
`CLAUDE_CODE_EFFORT_LEVEL` or `/effort`. For the lower cost of GLM 5.3 Flash, replace `atlas-dev` with
`atlas-code-fast` in the six model lines above. Thinking blocks of earlier turns are not sent back to the model,
so `context_management` edits of type `clear_thinking_*` are accepted; other context edits are refused.

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
    - The assistant profiles (`atlas`, `atlas-glm`, `atlas-deepseek`, `atlas-qwen`) answer in prose with their own tools and never guarantee a JSON format: any `response_format` other than `text` is refused with HTTP 400 `unsupported_parameter` (`param: "response_format"`). Use `atlas-chat`, `atlas-code` or `atlas-dev` for JSON output.
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

Each call is charged what the provider measured (its usage frame, cache reads at the cached rate).
Before the call, Atlas reserves its bound (the whole request plus `max_tokens`) against your budget;
at the end it charges the measured cost instead. When a call ends without the provider's usage (the
stream was cut, the client disconnected, a timeout), Atlas charges an estimate, never the reservation:
one token per 3 bytes of the request sent and of the text streamed back, at the uncached input rate.
A call the provider refused, or that failed before the first byte of an answer, costs nothing. Two
counters keep this visible: `unknown_micro_eur` is the charge of calls that ended without a
measurement, and `estimated_micro_eur` is the part of `charged_micro_eur` that no provider measured
(estimates, plus calls still running). The usage reported in such a failed call's response is absent;
use `GET /usage` to see what it cost.

| Limit | Value |
|---|---|
| Rate | 600 requests per minute per key (burst 200), and 1800 per minute per client address (burst 400). Everyone behind one office or VPN address shares the second limit: start parallel workers a few seconds apart. |
| Request size | up to 8 MiB, up to 4096 messages of history |
| Tools | up to 128 tools per request and 128 tool calls per message; a tool description may hold up to 200,000 characters (the request size limit still applies) |
| Output | 16384 tokens by default, at most 32768 |

## 8. Errors

### Error format

Chat Completions, Responses, `GET /models`, embeddings and the audio routes answer an error in the
OpenAI envelope. `type` holds the Atlas code of the table below (most errors repeat it in `code`): it
is the stable value to test in your code; `message` is for people and may change. Some errors add fields: `missing_scope`, `key_scopes`,
`param`, `measured`, `limit`, `unit`, `provider_status`, `retry_after`.

```json
{"error": {"type": "insufficient_scope", "code": "insufficient_scope",
  "message": "This API key is restricted: it lacks the model scope needed for direct models (...)",
  "param": "model", "missing_scope": "model", "key_scopes": ["assistant"]}}
```

Messages answers in the Anthropic envelope, `{"type": "error", "error": {"type": ..., "message": ...}}`,
where `error.type` is the Anthropic class of the status (400 `invalid_request_error`, 401
`authentication_error`, 403 `permission_error`, 404 `not_found_error`, 413 `request_too_large`, 429
`rate_limit_error`, 5xx `api_error`) and the Atlas code below is in `error.code`.

An error after a stream has started arrives as a last event: a `data: {"error": {...}}` chunk before
`[DONE]` on Chat Completions, `error` then `response.failed` on Responses, an `error` event on
Messages.

### Keys and scopes

Every key gives the same offer (owner rule): a new key has both scopes, `assistant` and `model`, and
the model name of each request chooses what Atlas does. A key has fewer scopes only when its creator
restricted it explicitly (an "Assistant only" or "Model access" key in the key page, or `--scope` for
the operator). A restricted key that asks for a usage outside its scopes gets 403
`insufficient_scope`; `missing_scope` names the scope, `key_scopes` lists the key's own. Create a key
without a restriction (or ask your administrator), then send the same request.

| Request | Scope it needs |
|---|---|
| `model`: `atlas` or an assistant profile, on Chat Completions, Responses or Messages | `assistant` |
| `model`: `atlas-dev`, `atlas-code-fast`, `atlas-chat`, `atlas-code`, a dated alias, a precise id of `GET /models`, or any other name (served as `atlas-dev`), on Chat Completions, Responses or Messages | `model` |
| `POST /embeddings`, `/audio/speech`, `/audio/transcriptions`, `/audio/translations`, `/responses/input_tokens`, `/messages/count_tokens` | `model` |
| `GET /models` | any key; the list shows the models the key's scopes allow |
| `GET /models/{id}` | the scope of that model (403 `insufficient_scope` for a known model outside the key's scopes, 404 `model_not_found` for an unknown one) |
| `GET /usage` | any key |
| Project fields or headers (`project_id`, `conversation_id`, `file_version_ids`, `X-Atlas-Project`, ...) | never available to keys: 403 `insufficient_scope` with `missing_scope: "project"` |

### Error codes

| Status | Code | Meaning | What to do |
|---|---|---|---|
| 400 | `invalid_request` | The body is not JSON, a field is invalid (the message names it, for example `unknown field messages.2.foo`), or the history has more than 4096 messages | Fix the request |
| 400 | `invalid_model` | `model` is not a string, or names nothing Atlas serves | Use a name from `GET /models` |
| 400 | `unsupported_parameter` (`param` names it) | An option no hosted model applies, for example `response_format` with the assistant or `json_schema` strict on a route that cannot guarantee it | Remove the option or use another model |
| 400 | `assistant_runs_its_own_tools` | Client tools were given to `atlas` or a profile on Responses or Messages (Chat Completions ignores them) | Use `atlas-dev` or `atlas-chat` for your own tools |
| 400 | `file_data_required`, `invalid_file_data`, `unreadable_file`, `unsupported_content` | An attached file is missing, not base64 or has no readable text, or a content type (`input_audio`) is not accepted | Check the file part |
| 400 | `storage_not_supported` | `previous_response_id`, `conversation` or `background`: Atlas stores nothing | Send the full history |
| 400 | `context_exceeded`, `input_limit` | The input is longer than the model accepts, or an embeddings request has too many inputs or bytes (`measured`, `limit`, `unit`) | Shorten or split the input |
| 400 | `cost_budget` | One call would cost more than the per-call cap (usually a huge `max_tokens` with a long input) | Lower `max_tokens` or the input |
| 400 | `translation_not_supported` | `/audio/translations` is not offered | Transcribe, then translate with a chat model |
| 400 | `web_search_unavailable_in_strict_privacy`, `web_fetch_unavailable_in_strict_privacy` | A hosted web tool was asked in strict privacy | Remove the tool or the strict level |
| 400 | `invalid_privacy_level` | `privacy` (field or `X-Atlas-Privacy` header) is not `standard`, `enhanced` or `strict` | Fix the value |
| 401 | `invalid_key_request` | No key, a key that does not start with `atl_`, two authorization headers, or a browser request (cookie or `Origin`) | Send one `Authorization: Bearer <key>` header from a server |
| 401 | `unauthorized` | Wrong, expired or revoked key, or its owner is no longer an active member | Check the key and its expiry date |
| 403 | `insufficient_scope` | A restricted key asked for a usage outside its scopes (see "Keys and scopes") | Use a key without a restriction |
| 403 | `strict_privacy_unavailable` | Strict privacy was asked and no owned server exists for that capability | Do not retry: remove the strict level, or ask the operator for an owned server |
| 403 | `enhanced_privacy_unavailable` | Enhanced privacy was asked and this server cannot pseudonymise yet | Do not retry: use another level |
| 403 | `privacy_feature_refused` | The feature in `feature` is not allowed at the applied `level` | Do not retry: remove the feature or use another level |
| 404 | `model_not_found` | `GET /models/{id}` for an unknown id | Use an id of `GET /models` |
| 404 | `storage_not_supported` | A stored response, conversation or completion was requested | Atlas stores nothing; keep your own history |
| 409 | `tool_loop_detected` | The same tool call returned the same result 12 times in a row | Change the approach; do not retry the same call |
| 413 (400 on some direct-model routes) | `body_limit` | The body is larger than the route accepts (`measured`, `limit`) | Send less, or split the request |
| 429 | `quota_exceeded` | The key's own daily or weekly limit is reached (the fields say which) | Wait for the next day or week, or ask for a higher limit |
| 429 | `budget_limit`, `cost_budget` (assistant) | The budget behind the key is reached (`used`, `limit`, `category`) | Stop, or ask for a higher budget |
| 429 | `rate_limit` (`provider_status`, `Retry-After`) | The model provider rate-limited the request | Retry after the delay |
| 429 | `provider_limit` | The transcription provider refused for quota or rate | Retry later |
| 429 | `owned_server_budget`, `owned_server_start_limit` | Strict privacy: the owned server's starter refused (budget cap, or too many starts this hour) | Retry after `retry_after` seconds, or use another level |
| 429 | (HTML page) | Rate limit of the front server: 600 requests per minute per key, 1800 per client address | Wait 30 to 60 seconds; do not restart all workers at once |
| 402 | `provider_quota` (`provider_status`) | The model provider's account is out of credit | Do not retry at once; tell the operator |
| 502 | `invalid_tool_arguments` | The model returned tool-call arguments that are not valid JSON, also after one corrected attempt (section 5) | Send the request again |
| 502 | `invalid_response_format` | The answer did not match the requested JSON schema, also after one corrected attempt | Send the request again, or simplify the schema |
| 502 | `provider_error` | The model provider failed in another way | Retry once, then report it |
| 503 | `provider_unavailable` (`provider_status`) | The model provider is unavailable | Retry later |
| 503 | `owned_server_starting` (`Retry-After`) | Strict privacy: the owned server is starting for this request (about 2 minutes) | Retry after `Retry-After` seconds |
| 503 | `key_service_unavailable`, `unavailable`, `tts_unavailable`, `transcription_unavailable` | A component of Atlas is unavailable | Retry later |
| 504 | `provider_timeout` | The model provider did not answer in time | Retry, with a shorter output if possible |
| 502 or 503 (no JSON) | | The service is restarting for a deployment (usually under a minute) | Wait and retry |

Loop thresholds, counted over the last 12 tool calls of the history (same tool, same arguments, same
result): at 3, a note asking the model to change approach is appended to the tool output; from 5, the
repeated output is replaced by that note and a short instruction follows it as a user message; at 12,
the request is refused with 409 `tool_loop_detected`. Calls whose results differ (tests rerun after an
edit) are never counted.

Atlas also repairs two unambiguous tool-call slips before your client sees them: a shell command sent
to an undeclared tool name (`bash`, `exec`, `shell`, ...) when the request declares exactly one shell
tool, and a declared shell tool called with `command` instead of its required `cmd` (or the reverse).
Arguments that are not valid JSON are repaired or asked again (section 5); a call to a tool you did
not declare is passed through, so that your client can answer that the tool is unknown.

## 9. Privacy

Requests are served by open-weight models hosted by European providers (Mistral and Scaleway). Treat
the service as a beta and do not send data you could not share with a hosting provider.

### Privacy levels

Every request is served at one of three levels:

| Level | What leaves Atlas |
|---|---|
| `standard` (default) | Request content goes to the European providers of the active routes. |
| `enhanced` | Identifiers detected by Atlas are to be replaced by tokens before content leaves Atlas (pseudonymisation). Not offered yet: see below. |
| `strict` | Nothing leaves machines operated by Atlas or by the customer (a rented dedicated server counts). No web search. |

Choose the level with the `privacy` body field (`"privacy": "strict"`, on `/chat/completions`,
`/responses`, `/messages` and `/embeddings`) or the `X-Atlas-Privacy` header. The applied level is the
highest of the organisation minimum, the key's default, the header and the field: a request can raise
it, never lower it. Every response carries the applied level in `X-Atlas-Privacy-Applied`. The field
is removed before anything is sent to a provider.

A level that cannot be honoured is refused before anything is sent, with HTTP **403** and a named code
(`strict_privacy_unavailable`, `enhanced_privacy_unavailable` or `privacy_feature_refused`; the body
also names `feature` and `level`). These refusals are permanent: OpenAI-compatible SDKs retry 5xx
answers automatically, so a policy refusal is never a 5xx. (Until 2026-11-14 strict refusals answered
503; the code `strict_privacy_unavailable` is unchanged.) A 503 stays reserved for a temporary outage,
such as a restart.

Enhanced: the local pseudonymiser is not installed yet, so the level never pretends to protect
anything. Where it is not offered (the default) a request asking for it is refused with 403
`enhanced_privacy_unavailable`. Where an operator offers it before the pseudonymiser exists, model calls
use owned servers only (or are refused by the same code), and features whose content cannot be
pseudonymised are refused with `privacy_feature_refused`: images and scanned pages (`vision`, `ocr`),
dictation and transcription audio, embeddings, web search and fetch. Connectors stay allowed (the user
chose that recipient).

### How privacy levels are configured (operator)

Levels have no settings screen. In the chat, each conversation shows its level next to the composer
and the user picks it there; a conversation that already has messages can be raised, never lowered.
`ATLAS_PRIVACY_MINIMUM` (`standard`, `enhanced` or `strict`; an unreadable value counts as `strict`) is
the organisation minimum, applied to every request, conversation, project and key.
`ATLAS_PRIVACY_ENHANCED=1` offers the Enhanced level (off by default until the pseudonymiser exists).

Which routes accept strict privacy is part of the deployment: targets
marked `"privacy": "owned"` in `services/model-gateway/api-routing.json`, each pointing to an
OpenAI-compatible vLLM server that the owner fully controls, whose address is set with
`ATLAS_OWNED_VLLM_BASE_URL` (and `ATLAS_OWNED_VLLM_API_KEY` when the server needs one). A request opts in
with the `X-Atlas-Privacy: strict` header; an operator can also make a key strict when issuing it, and a
project can be set to strict through the project API. In strict mode web search and web fetch are not
offered. See the strict privacy section of [the platform API notes](08-plateforme-api.md).

An owned server may be powered off between uses (an on-demand GPU). Where the operator installs a
starter for it (`ATLAS_OWNED_SERVER_STARTER_DIR`, a directory where Atlas drops a start request and
reads the starter's `status.json`; the starter alone holds the cloud credentials and enforces its caps),
a strict request that finds the server silent starts it and gets 503 `owned_server_starting` with
`Retry-After`; a refusal of the starter is a named 429 with its measured values, never an outage.
Without a starter, nothing changes. Shared traffic never starts the owned server.
