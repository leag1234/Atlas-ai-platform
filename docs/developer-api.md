# Atlas developer API (hosted)

Atlas is in beta. This guide is for developers who call the hosted Atlas API with a personal or
project key, from scripts, applications or coding agents. For the self-hosted developer API and the
full list of supported OpenAI formats, see [API compatibility](api-compatibility.md).

## 1. Quick start

| Item | Value |
|---|---|
| Base URL | `https://atlas.eu.com/api/atlas/v1` |
| Authentication | `Authorization: Bearer <key>` (or `x-api-key: <key>`) |
| Models | `atlas-code` (coding and agents), `atlas-chat` (general conversation) |
| Formats | OpenAI Chat Completions (`/chat/completions`), OpenAI Responses (`/responses`), Anthropic Messages (`/messages`), all with streaming |
| Other routes | `GET /models`, `GET /usage` |

Atlas chooses the open-weight model that serves a request. The response fields `served_by` and
`privacy` tell you which one answered.

Keep the key in an environment variable. Never commit it, print it, paste it in a prompt, a ticket or
a log, or share it with another person: each key has its own budget and its own audit trail.

```sh
export ATLAS_API_KEY="$(cat ~/path/to/your.key)"

curl -s https://atlas.eu.com/api/atlas/v1/chat/completions \
  -H "Authorization: Bearer $ATLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"atlas-code","messages":[{"role":"user","content":"Hello"}]}'
```

Any OpenAI-compatible client works. With the Python SDK:

```python
import os
from openai import OpenAI

client = OpenAI(base_url="https://atlas.eu.com/api/atlas/v1", api_key=os.environ["ATLAS_API_KEY"])
reply = client.chat.completions.create(
    model="atlas-code",
    messages=[{"role": "user", "content": "Explain a mutex in two sentences."}],
)
print(reply.choices[0].message.content)
```

Streaming works as usual (`"stream": true`). Add `"stream_options": {"include_usage": true}` to
receive token usage in the final chunk before `[DONE]`.

## 2. Models, reasoning effort and cost

### Which model

| Model | Use it for | effort `none` (default) | effort `low`, `medium`, `high` |
|---|---|---|---|
| `atlas-code` | development: code, agents, tool calls | GLM 5.3 Flash | GLM 5.3 |
| `atlas-chat` | everything else: conversation, writing, analysis | GLM 5.3 Flash | GLM 5.3 |

Both models try the same providers first. They differ only in the fallback used when the first provider
is unavailable: DeepSeek V4 Flash for `atlas-code`, GLM 5.2 for `atlas-chat`. `served_by` in the response
names the route that answered. The context window is 1,048,576 tokens on the first route and 256,000 on
the fallback. `GET /models` also lists dated aliases (`atlas-code-2026-10-30`, `atlas-chat-2026-10-30`)
that name the same routes.

### Which reasoning effort

`reasoning_effort` accepts `none`, `low`, `medium` and `high`, in increasing order. `none` means no
reasoning and is the default when the field is omitted. Moving from `none` to `low` also changes the
model: the cheaper GLM 5.3 Flash becomes the full GLM 5.3, which costs about nine times more per token.
`low`, `medium` and `high` use the same model and differ in how long it reasons.

Measured on 2026-11-02 with `atlas-code` (15 calls per level: counting, time arithmetic, a coin-change
count, and two Python functions checked by unit tests):

| Effort | Correct | Average charge per call |
|---|---|---|
| `none` | 13 of 15 | 0.000036 EUR |
| `low` | 15 of 15 | 0.000301 EUR |
| `medium` | 15 of 15 | 0.000326 EUR |
| `high` | 15 of 15 | 0.000343 EUR |

The two failures at `none` were the interval-merging function. On tasks this short, `low`, `medium` and
`high` were not distinguishable in quality: start at `none`, move to `low` when tests fail, and keep
`high` for hard diagnosis where `low` and `medium` did not work.

### What it costs

Prices per million tokens, in euros excluding tax:

| Model | Input | Output |
|---|---|---|
| GLM 5.3 Flash (`none`) | 0.1275 | 0.425 |
| GLM 5.3 (`low` and above) | 1.19 | 3.74 |
| DeepSeek V4 Flash (`atlas-code` fallback) | 0.40 | 0.80 |
| GLM 5.2 (`atlas-chat` fallback) | 1.80 | 5.50 |

Agents resend their whole context at every step. Mistral caches the repeated start of a prompt and
bills cached input tokens much less (GLM 5.3: 0.119 EUR per million instead of 1.19; GLM 5.3 Flash:
0.0255 instead of 0.1275). Atlas passes this through: cached tokens appear in
`usage.prompt_tokens_details.cached_tokens` and are charged at the cached rate. Measured on 2026-11-02
with a 10,162-token prompt repeated five times:

| Route | First call | Next calls |
|---|---|---|
| GLM 5.3 (`low` and above) | 0.0121 EUR | 0.0013 EUR (about 89% less), from the second call |
| GLM 5.3 Flash (`none`) | 0.0013 EUR | 0.0005 EUR when the cache hit, which happened on 2 of 5 calls |

An agent step that sends 50,000 tokens of context and receives 500 tokens costs about 0.0066 EUR at
`none` with no cache hit. At `low` or above it costs about 0.061 EUR when the context is not cached and
about 0.0078 EUR when it is, which is the usual case after the first step of a run. Over 100 steps that is
roughly 0.66 EUR at `none`, 6.1 EUR without cache at `low`, and 0.78 EUR with the cache. Measured on whole
agent tasks (2026-10-31): about 3 EUR at `none` and about 14 EUR at `high`.

To benefit from the cache, keep the beginning of your prompts identical from one request to the next
(system prompt, tool definitions, earlier turns) and put what changes at the end.

### `max_tokens`

Reasoning tokens count toward `max_tokens`. If the budget is too small, the model spends all of it
thinking and returns no answer; Atlas retries and then answers `503 key_service_unavailable`, which is
a misleading name for this case. With any effort above `none`, set `max_tokens` to at least 1000 (or
leave it out). When `max_tokens` is omitted, the default is 16384 and the maximum is 32768.

## 3. Tool calls

Function definitions, tool-call history, tool results and `tool_choice` (`auto`, `none`, `required` or a
named function) are supported on Chat Completions and Responses. Your client executes the tools; Atlas
never runs them for you.

```sh
curl -s https://atlas.eu.com/api/atlas/v1/chat/completions \
  -H "Authorization: Bearer $ATLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"atlas-code","tool_choice":"required",
       "tools":[{"type":"function","function":{"name":"get_time","description":"Get the time",
         "parameters":{"type":"object","properties":{"tz":{"type":"string"}},"required":["tz"]}}}],
       "messages":[{"role":"user","content":"What time is it in Paris?"}]}'
```

## 4. Coding agents

### Codex CLI

Use a dedicated Codex home for Atlas, separate from any other login, for example
`export CODEX_HOME=~/.codex-atlas`, with this `config.toml`:

```toml
model = "atlas-code"
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
atlas-code not found" is expected. Keep the two context lines: without them Codex never compacts a long
session. Agents resend their whole context at every step, so a lower compaction limit lowers the cost
of long tasks.

### Claude Code

```sh
export ANTHROPIC_BASE_URL=https://atlas.eu.com/api/atlas/v1
export ANTHROPIC_AUTH_TOKEN="$ATLAS_API_KEY"
unset ANTHROPIC_API_KEY
export ANTHROPIC_MODEL=atlas-code
export ANTHROPIC_DEFAULT_OPUS_MODEL=atlas-code
export ANTHROPIC_DEFAULT_SONNET_MODEL=atlas-code
export ANTHROPIC_DEFAULT_HAIKU_MODEL=atlas-code
export CLAUDE_CODE_SUBAGENT_MODEL=atlas-code
export CLAUDE_CODE_DISABLE_THINKING=1
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export ENABLE_TOOL_SEARCH=false
claude --model atlas-code
```

Extended thinking, image input and hosted tools (web search, code execution) are not available
through Atlas.

### Running agents cost-effectively

- Bound every task: one clear goal, a time limit and a turn limit. A worker that rereads the same files
  in a loop burns budget without progress: stop it.
- One writer per file or git branch. Two agents never edit the same files.
- An agent task costs a few euros at effort `none`, and several times more at `high`. Watch
  `GET /usage` during the first runs.

## 5. Budget, limits and usage

Every key has its own budget, set when it was issued: a weekly or daily amount in euros, and an
expiry date. Check where you stand at any time:

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

## 6. Errors

| Status | Meaning | What to do |
|---|---|---|
| 400 `invalid_request` or `body_limit` | A field is invalid (for example an unknown `reasoning_effort`), the body is larger than 8 MiB, or the history has more than 4096 messages | Fix or shorten the request |
| 401 | Missing, wrong, expired or revoked key | Check the key and its expiry date |
| 409 `tool_loop_detected` | The same tool call returned the same result 12 times in a row | Change the approach; do not retry the same call |
| 429 with a JSON body | The budget of the key is reached; the message says whether day or week | Stop, or ask for a higher budget |
| 429 with an HTML page | Rate limit | Wait 30 to 60 seconds and resume; do not restart all workers at once |
| 502 or 503 | The service is restarting for a deployment (usually under a minute) | Wait and retry |
| 503 `strict_privacy_unavailable` | You asked for strict privacy and no owned inference server exists yet | Remove the `X-Atlas-Privacy: strict` header |
| 503 `key_service_unavailable` | A downstream failure; a common cause is a `max_tokens` too small for the reasoning effort | See section 2, then retry |

To discourage loops, Atlas also appends a note asking the model to change approach when the same tool
call returns the same result several times, before it stops the exchange at the twelfth.

## 7. What the API does not do

The hosted developer API takes text and function calls. It has no file upload, no embeddings, no image
input or generation, no web search, no stored responses (`previous_response_id`) and no access to the
web assistant's conversations, documents or projects. Pass the content you need in the request.

## 8. Privacy

Requests are served by open-weight models hosted by European providers; `served_by` names the exact
route. A strict privacy mode (`X-Atlas-Privacy: strict`), which would only use inference servers fully
controlled by the owner, exists but is not available until such a server is configured: until then it
answers 503 and never falls back to a shared provider. Treat the service as a beta and do not send data
you could not share with a hosting provider.

### How strict privacy is configured (operator)

Strict privacy has no settings screen. Which routes accept it is part of the deployment: targets
marked `"privacy": "owned"` in `services/model-gateway/api-routing.json`, each pointing to an
OpenAI-compatible vLLM server that the owner fully controls, whose address is set with
`ATLAS_OWNED_VLLM_BASE_URL` (and `ATLAS_OWNED_VLLM_API_KEY` when the server needs one). A request opts in
with the `X-Atlas-Privacy: strict` header; an operator can also make a key strict when issuing it, and a
project can be set to strict through the project API. In strict mode web search and web fetch are not
offered. See the strict privacy section of [the platform API notes](08-plateforme-api.md).
