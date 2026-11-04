# Atlas API coverage

What the hosted Atlas API implements of the OpenAI and Anthropic APIs, and what it does not. State as of
2026-11-03. The goal is the full OpenAI and Anthropic APIs; this page is the list of what remains. For
how to call the API, see the [Atlas API guide](developer-api.md).

Legend:
- **Applied**: sent to the model and effective.
- **Accepted, no effect**: answers 200 and is not applied. Atlas decides this itself, for a reason given below.
- **Dropped**: answers 200 and the field is ignored without an error. These are gaps to close: the target is an error that names the field.
- **Refused**: answers 400 and the error names the field.
- **Not implemented**: the route or feature does not exist.

Atlas stores nothing: no response, conversation or file is kept after the request. Every feature that
needs server-side storage is listed as not implemented and answers `storage_not_supported` when it is
called.

The parameter tables describe the direct models (`atlas-dev`, `atlas-chat`). The assistant (`atlas` and
its profiles) runs its own tools and settings: it takes `model`, `messages` or `input`, `stream`,
`max_tokens`, `reasoning_effort` and `seed` (measured on 2026-11-02), accepts the other fields, and
refuses client tools with `assistant_runs_its_own_tools`. On Messages, `stop_sequences` end its final
text at the first match, with `stop_reason: "stop_sequence"` and the sequence.

## 1. Routes

| Route | OpenAI | Anthropic |
|---|---|---|
| Chat Completions: `POST /chat/completions` | Applied | n/a |
| Responses: `POST /responses` | Applied | n/a |
| Messages: `POST /messages` | n/a | Applied |
| Count input tokens: `POST /responses/input_tokens`, `POST /messages/count_tokens` | Applied (tokens estimated as bytes / 4) | Applied (same estimate) |
| Models: `GET /models`, `GET /models/{id}` | Applied | Applied (Anthropic format, paginated) |
| Retrieve, delete, cancel a response; list its input items; compact | Not implemented (nothing is stored) | n/a |
| Conversations (`/conversations`, items) | Not implemented (nothing is stored) | n/a |
| Stored Chat Completions: list, retrieve, update, delete, messages | Not implemented (nothing is stored) | n/a |
| Message Batches | n/a | Not implemented |
| Files API | Not implemented | Not implemented |
| Embeddings: `POST /embeddings` | Applied (model `atlas-embed`, see 4 bis) | n/a |
| Images: generations, edits, variations | Not implemented | n/a |
| Audio: speech (`POST /audio/speech`, local service) | Applied (`atlas-tts` when configured) | n/a |
| Audio: transcriptions (`POST /audio/transcriptions`) | Applied (`atlas-transcribe` when configured, see 4 ter; `srt`, `vtt`, `prompt` and word timestamps refused) | n/a |
| Audio: translations (`POST /audio/translations`) | Refused by name (`translation_not_supported`) | n/a |
| Moderations | Not implemented | n/a |
| Batch, Uploads, Fine-tuning, Evals, Vector stores | Not implemented | n/a |
| Assistants, Threads, Runs (deprecated), Realtime, Containers | Not implemented | n/a |
| Skills, Admin API | n/a | Not implemented |
| `GET /usage` (budget and counters of the key) | Atlas addition | Atlas addition |

## 2. Chat Completions parameters

| State | Parameters |
|---|---|
| Applied | `messages`, `model`, `temperature`, `top_p`, `stop`, `seed`, `frequency_penalty`, `presence_penalty`, `max_tokens`, `max_completion_tokens`, `n` (up to 8 choices), `logprobs`, `top_logprobs`, `response_format`, `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning_effort`, `stream`, `stream_options`, `web_search_options` (hosted web search), legacy `functions` and `function_call` (mapped to tools) |
| Accepted, no effect | `store` (reported as false), `metadata`, `user`, `safety_identifier`, `service_tier`, `verbosity`, `prompt_cache_key`, `prompt_cache_retention` |
| Refused | `logit_bias` (no hosted provider applies it), `audio`, `modalities`, `prediction`, `moderation`, `prompt_cache_options` |

| Message content | State |
|---|---|
| Text, `image_url`, `file` (base64) parts | Applied |
| `developer` role, `refusal` parts, legacy `function` role | Applied (mapped to `system`, text and tool results) |
| `input_audio` | Refused by name (no hosted model takes audio) |
| `file_id`, `file_url` references | Refused (send the content) |
| `custom` tool type | Not implemented on this format |
| `url_citation` annotations (with web search), streamed or not | Applied |
| `reasoning_content` in streamed deltas, when the provider returns reasoning | Applied |
| Audio output, `reasoning_content` in non-streamed answers | Not implemented |

## 3. Responses parameters and items

| State | Parameters |
|---|---|
| Applied | `model`, `input`, `instructions`, `max_output_tokens`, `temperature`, `top_p`, `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning.effort`, `text.format`, `stream`, `metadata` (echoed back) |
| Accepted, no effect | `store` (reported as false), `include`, `background: false`, `context_management`, `max_tool_calls`, `prompt_cache_key`, `prompt_cache_retention`, `safety_identifier`, `service_tier`, `stream_options`, `top_logprobs`, `truncation`, `user`, `reasoning.summary` |
| Refused (`storage_not_supported`) | `previous_response_id`, `conversation`, `background: true`, `prompt` (stored prompt templates), `item_reference` input items |

| Tool or item | State |
|---|---|
| `function`, `custom`, `namespace` tools | Applied (executed by the client) |
| `local_shell`, `shell`, `apply_patch` tools | Applied (executed by the client) |
| `web_search` tool | Applied (executed by Atlas, up to 4 turns, `web_search_call` items, `url_citation` annotations) |
| `code_interpreter` tool (`container.type: "auto"` only; explicit containers refused; `max_uses` 1–4, default 4) | Applied (executed by Atlas in a per-call sandbox) |
| `file_search`, `image_generation`, `mcp`, `computer_use_preview` tools | Refused by name (`invalid_request`, `unsupported tool: <type>`) |
| Input: text, `input_image`, `input_file`, function and custom call results, `reasoning` items | Applied |
| Input: `input_audio` | Refused by name |
| Output: messages, `function_call`, `custom_tool_call`, `local_shell_call`, `shell_call`, `apply_patch_call`, `web_search_call` | Applied |
| Output: `reasoning` items with `reasoning_text` content, when the provider returns reasoning (no `summary`, no `encrypted_content`) | Applied |
| Output: `code_interpreter_call` (`container_id: null`, bounded logs) | Applied |
| Output: `file_search_call`, `image_generation_call`, `mcp_call` | Not implemented |
| Stream events: `response.created`, `in_progress`, `output_item.added` and `done`, `content_part.added` and `done`, `output_text.delta` and `done`, function and custom tool input deltas, `web_search_call.completed`, `code_interpreter_call.in_progress/interpreting/completed`, `code_interpreter_call_code.delta/done`, `response.completed`, `response.failed` | Applied |
| Stream events: `reasoning_text.delta` and `done` | Applied |
| Stream event `output_text.annotation.added` (one per `url_citation`, before `output_text.done`) | Applied |
| Stream events for reasoning summaries, refusal, other hosted tools, audio | Not implemented |

## 4. Messages parameters and blocks

| State | Parameters |
|---|---|
| Applied | `model`, `messages`, `system`, `max_tokens`, `temperature`, `top_p`, `top_k`, `stop_sequences`, `tools`, `tool_choice`, `stream`, `output_config.effort` (sets the reasoning effort), `thinking` (`enabled` with `budget_tokens` from 1024 to below `max_tokens`: up to 4096 low, up to 16384 medium, above high; `adaptive`: medium; `output_config.effort` wins), `metadata.user_id` |
| Accepted, no effect | `cache_control` (Atlas caches repeated prompt starts by itself), `service_tier` |
| Refused | `container`, `inference_geo`, `speed` |

| Block or tool | State |
|---|---|
| `text`, `image` (base64 or url), `document` (text, PDF, content), `search_result` | Applied |
| `tool_use`, `tool_result` | Applied |
| `thinking`, `redacted_thinking` blocks in the history | Accepted and removed |
| `thinking` blocks in answers, when `thinking` is requested and the model reasons (signed with an opaque `signature`; streamed as `thinking_delta` then `signature_delta`, closed before the next block) | Applied |
| `web_search` server tool (`web_search_20250305` and later): `server_tool_use`, `web_search_tool_result`, `usage.server_tool_use` | Applied |
| `web_fetch` server tool (`web_fetch_20250910` and later): `server_tool_use`, `web_fetch_tool_result` (text document) or `web_fetch_tool_error`, `usage.server_tool_use.web_fetch_requests`; `max_uses`, `allowed_domains`, `blocked_domains`, `max_content_tokens`. Only addresses present in the conversation or in earlier search results are read (`url_not_allowed` otherwise); `citations` enabled refused by name; refused in strict privacy | Applied |
| `code_execution` server tool (`code_execution_20250825` and `code_execution_20250522`, named `code_execution`; `max_uses` 1–4, default 4): `server_tool_use`, `code_execution_tool_result` or `code_execution_tool_result_error`, `usage.server_tool_use.code_execution_requests` | Applied (executed by Atlas in a per-call sandbox) |
| `bash_code_execution`, `text_editor_code_execution`, memory, computer use, MCP connector, `tool_search` | Not implemented |
| Citations on text blocks: `web_search_result_location` when the answer quotes a searched page (streamed as `citations_delta`); citations sent back in history are accepted | Applied |
| Citations on document blocks (`citations.enabled` on a document) | Not implemented |
| Stream events: `message_start`, `content_block_start`, `content_block_delta` (`text_delta`, `input_json_delta`), `content_block_stop`, `message_delta`, `message_stop` | Applied |
| `stop_reason: "stop_sequence"` with the sequence that stopped the text | Applied when the provider names the sequence (models served by Scaleway); `end_turn` otherwise (the Mistral API does not say which sequence matched) |

## 4 bis. Embeddings

| State | Parameters |
|---|---|
| Applied | `input` (a string or up to 2048 strings, each at most 131072 bytes), `encoding_format` (`float` or `base64`), `dimensions` (32 to 4096; the provider default is 4096) |
| Accepted, no effect | `user` |
| Refused by name | token arrays as `input`, any other field |

- **Model.** Every model name is served by `atlas-embed` (a multilingual embedding model hosted in France); `model` in the answer says `atlas-embed`.
- **Price and limits.** Input tokens at the embedding role's price; the key's daily quota and budget apply as for chat.
- **Strict privacy.** Refused (`strict_privacy_unavailable`): no owned embedding server yet.
- **Nothing is stored**, as everywhere else.

## 5. Differences that are not gaps in the list above

- **Model names.** A model name Atlas does not know (`gpt-5-codex`, `claude-sonnet-5-5`, a typo) is served as `atlas-dev` on the direct formats. Check `model` and `served_by` in the first responses.
- **Tokens.** `input_tokens` counts of the two counting routes are estimates, not a provider tokenizer.
- **Unknown fields.** A field that is not part of the format answers 400 and the message names it, for example `unknown field messages.2.foo`.
- **Strict privacy.** The hosted `web_search` tool is refused in strict privacy mode (`web_search_unavailable_in_strict_privacy`).
- **Code execution.** Each execution runs Python in a new container: no network, no mounted files, read-only root, 1 CPU, 1 GB, 64 processes, 256 MB `/tmp`, 60 s, 64 KB of stdout and of stderr. At most 4 executions per request; a per-key daily cap (`ATLAS_API_CODE_EXECUTION_DAILY_LIMIT`, default 100) answers `code_execution_daily_limit`. Allowed in strict privacy (it runs on Atlas machines).

## 6. Open items, in the order planned

1. Files and images generated by code execution (code interpreter phase 2: today only stdout, stderr and the exit code come back; nothing persists between executions).
2. Citations on document blocks.
3. The other families outside chat (images, audio, files, batches): phase 2.
