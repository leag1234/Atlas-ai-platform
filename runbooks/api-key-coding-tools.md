# Coding tools with Atlas keys

Use an Atlas key with **model** scope. An assistant-only key receives 403.
The public OpenAI base is `https://atlas.eu.com/api/atlas/v1`.
Claude Code uses the same base and appends `/v1/messages`, so the complete URL is
`https://atlas.eu.com/api/atlas/v1/v1/messages`. This repeated `v1` is intentional.
Clients that append `/messages` can use the shorter alias at
`https://atlas.eu.com/api/atlas/v1/messages`. Both paths have `/count_tokens`.
Alternatively Claude Code can use `ANTHROPIC_BASE_URL=https://atlas.eu.com/api/atlas`;
appending `/v1/messages` then reaches the shorter alias.
The version header `anthropic-version: 2023-06-01` is accepted.

Load the private Atlas key into the client environment using your secret manager.
The examples assume `ATLAS_API_KEY` already contains it. Do not put credentials in
tracked configuration, command arguments, debug HTTP traces or shell history.

## Claude Code

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
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=512
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
claude --model atlas-code
```

Alternatively unset `ANTHROPIC_AUTH_TOKEN` and set
`ANTHROPIC_API_KEY="$ATLAS_API_KEY"` to send `x-api-key`. Authorization takes
precedence when both headers are present. Duplicate credential headers, browser
Origin and Cookie headers are rejected. Keep the client's normal tool approvals.

The Messages bridge reuses the developer API's text and function adapters.
Tool use is executed by the client; Atlas forwards tool results to the selected
raw model. JSON and incremental SSE are supported. Thinking, images, hosted tools
and other unsupported extensions are rejected, not silently executed. Cache hints
on supported text/result blocks and tool definitions are accepted without a cache guarantee.
If the normal initial context exceeds the request budget, use a smaller task/context
and output limit; the bridge never silently truncates input.

`atlas-code` and unrecognized client model strings, including `claude-*` and `gpt-*`,
select the configured default raw model on Messages routes. Operators may set
`ATLAS_API_DEFAULT_RAW_MODEL=raw/<configured-provider-model-id>` on the adapter;
when unset, it selects the gateway code role. Explicit configured `raw/` IDs from
`GET /models` override that default. The response identifies the selected raw ID.
This routing does not imply the requested vendor model is actually served.

Every inference uses the same key request/money ceilings and user cost ledger as
raw Chat Completions. Expired/revoked keys return 401, missing scope 403 and quota
or budget exhaustion 429, all in Anthropic error envelopes. A failed stream emits
an `error` event; uncertain provider costs retain the reservation. Token counting
returns a conservative local estimate (serialized UTF-8 input bytes plus 512), not
an exact provider tokenization. It performs no inference and consumes no request
or monetary quota. Actual billing uses inference usage. Body limit: 1MiB;
request deadline: 120 seconds; configured context and EUR 0.05 call ceiling apply.

Sources: [Claude gateway setup](https://code.claude.com/docs/en/llm-gateway-connect),
[model settings](https://code.claude.com/docs/en/model-config),
[wire compatibility](https://code.claude.com/docs/en/llm-gateway-protocol).
Native client acceptance is performed by the supervisor after deployment.

## Codex CLI (companion Responses change required)

The Messages change does not install the public Responses route. Use this profile
only after the companion Responses change has been integrated and deployed.
In `~/.codex/config.toml`:

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

[features]
enable_request_compression = false
```

Codex appends `/responses`. Codex 0.153 requires `wire_api = "responses"`;
`chat` is unsupported. Authentication comes from `ATLAS_API_KEY`.
Source: [official OpenAI configuration](https://learn.chatgpt.com/docs/config-file/config-advanced).

## Aider (Chat Completions)

```sh
export OPENAI_API_BASE=https://atlas.eu.com/api/atlas/v1
export OPENAI_API_KEY="$ATLAS_API_KEY"
aider --model openai/atlas-code
```

Aider sends `/chat/completions`; its `openai/` prefix selects the client provider.
This route already supports `atlas-code` and advertised configured raw IDs; the
Messages unknown-name fallback does not change Chat Completions validation.
Source: [Aider OpenAI-compatible setup](https://aider.chat/docs/llms/openai-compat.html).
