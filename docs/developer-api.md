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

## 2. Reasoning effort and `max_tokens`

`reasoning_effort` accepts `none` (default, fastest and cheapest), `low`, `medium` and `high`. Use
`high` only for hard diagnosis: it costs several times more.

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
| 400 `invalid_request` | A field is invalid (for example an unknown `reasoning_effort`) | Fix the request |
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
