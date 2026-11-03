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
refuses client tools with `assistant_runs_its_own_tools`.

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
| Embeddings | Not implemented | n/a |
| Images: generations, edits, variations | Not implemented | n/a |
| Audio: speech, transcriptions, translations | Not implemented | n/a |
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
| Audio output, `reasoning_content` in answers | Not implemented |

## 3. Responses parameters and items

| State | Parameters |
|---|---|
| Applied | `model`, `input`, `instructions`, `max_output_tokens`, `temperature`, `top_p`, `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning.effort`, `text.format`, `stream`, `metadata` (echoed back) |
| Accepted, no effect | `store` (reported as false), `include`, `background: false`, `context_management`, `max_tool_calls`, `prompt_cache_key`, `prompt_cache_retention`, `safety_identifier`, `service_tier`, `stream_options`, `top_logprobs`, `truncation`, `user`, `reasoning.summary` |
| Refused (`storage_not_supported`) | `previous_response_id`, `conversation`, `background: true` |
| Dropped | `prompt` (stored prompt templates), `item_reference` input items |

| Tool or item | State |
|---|---|
| `function`, `custom`, `namespace` tools | Applied (executed by the client) |
| `local_shell`, `shell`, `apply_patch` tools | Applied (executed by the client) |
| `web_search` tool | Applied (executed by Atlas, up to 4 turns, `web_search_call` items, `url_citation` annotations) |
| `file_search`, `code_interpreter`, `image_generation`, `mcp`, `computer_use_preview` tools | Dropped |
| Input: text, `input_image`, `input_file`, function and custom call results, `reasoning` items | Applied |
| Input: `input_audio` | Refused by name |
| Output: messages, `function_call`, `custom_tool_call`, `local_shell_call`, `shell_call`, `apply_patch_call`, `web_search_call` | Applied |
| Output: `reasoning` items, `file_search_call`, `code_interpreter_call`, `image_generation_call`, `mcp_call` | Not implemented |
| Stream events: `response.created`, `in_progress`, `output_item.added` and `done`, `content_part.added` and `done`, `output_text.delta` and `done`, function and custom tool input deltas, `web_search_call.completed`, `response.completed`, `response.failed` | Applied |
| Stream events for reasoning, refusal, annotations, other hosted tools, audio | Not implemented |

## 4. Messages parameters and blocks

| State | Parameters |
|---|---|
| Applied | `model`, `messages`, `system`, `max_tokens`, `temperature`, `top_p`, `top_k`, `stop_sequences`, `tools`, `tool_choice`, `stream`, `output_config.effort`, `metadata.user_id` |
| Accepted, no effect | `cache_control` (Atlas caches repeated prompt starts by itself), `service_tier`, `thinking: {"type": "disabled"}` |
| Refused | `thinking` enabled with a budget, `container`, `inference_geo`, `speed` |

| Block or tool | State |
|---|---|
| `text`, `image` (base64 or url), `document` (text, PDF, content), `search_result` | Applied |
| `tool_use`, `tool_result` | Applied |
| `thinking`, `redacted_thinking` blocks in the history | Accepted and removed |
| `thinking` blocks in answers | Not implemented |
| `web_search` server tool (`web_search_20250305` and later): `server_tool_use`, `web_search_tool_result`, `usage.server_tool_use` | Applied |
| `web_fetch`, code execution (`code_execution`, `bash_code_execution`, `text_editor_code_execution`), memory, computer use, MCP connector, `tool_search` | Not implemented |
| Citations on text blocks | Not implemented |
| Stream events: `message_start`, `content_block_start`, `content_block_delta` (`text_delta`, `input_json_delta`), `content_block_stop`, `message_delta`, `message_stop` | Applied |
| `stop_reason: "stop_sequence"` with the sequence that stopped the text | Not implemented (answers `end_turn`) |

## 5. Differences that are not gaps in the list above

- **Model names.** A model name Atlas does not know (`gpt-5-codex`, `claude-sonnet-5-5`, a typo) is served as `atlas-dev` on the direct formats. Check `model` and `served_by` in the first responses.
- **Tokens.** `input_tokens` counts of the two counting routes are estimates, not a provider tokenizer.
- **Unknown fields.** A field that is not part of the format answers 400 and the message names it, for example `unknown field messages.2.foo`.
- **Strict privacy.** The hosted `web_search` tool is refused in strict privacy mode (`web_search_unavailable_in_strict_privacy`).

## 6. Open items, in the order planned

1. Refuse by name what is dropped today: `prompt`, `item_reference`, and the Responses tools `file_search`, `code_interpreter`, `image_generation`, `mcp`, `computer_use_preview`.
2. Code interpreter on Responses and Messages (needs a container per request, see the terminal isolation work).
3. `reasoning` output items and `thinking` blocks in answers.
4. `stop_reason: "stop_sequence"` on Messages.
5. Anthropic `web_fetch`, citations, and the remaining Responses stream events.
6. The families outside chat (embeddings, images, audio, files, batches): phase 2.
