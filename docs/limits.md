# Production limits audit — M25

Status: production inventory and numerical boundary audit recorded below; local
verify-m25 completed with exit0; GitHub CI remains the certification authority. A resource rationale does
not prove the chosen numerical value optimal; it makes the tradeoff reviewable.
All sizes are bytes unless marked characters, tokens, pixels or items.

## Removed inherited ceilings

| Value | Enforcement | Decision and reason |
|---|---|---|
| 2000 (2 000) web-text bytes | orchestrator/tools.py | Removed in M21; complete content is summarised against a token budget. |
| 400 RAG characters | retrieval/tool.py | Removed in M21; select whole chunks. |
| 6 MB / 6291456 chat body | orchestrator/chat_api.py | Removed in M23; 32 MiB bounds decoding memory and supports multiple attachments. The independent image-upload endpoint retains 6 MiB for encoded images. |
| 32000 (32 000), later600000 aggregate history characters | packages/images.py | Removed in M25; estimate tokens against the active configured window, output reservation and8192-token tool allowance. |
| 200000 characters per text part/message | packages/images.py | Removed in M25; same history token accounting. |
| 32000 latest-question characters | orchestrator/loop.py Query | Removed in M25; public history/window and transport bounds apply. |
| 150 answer words | prompts/chat-agent.txt | Removed in M25; answer length follows the task and output reservation. |

## Shared constraints

| Values | Enforcement (relative to services/ unless packages/) | Why / classification |
|---|---|---|
| Context262144/256000/131072; embedding32768 tokens | model-gateway/routing.yaml capabilities; packages/profiles.py | Justified: configured provider capacities; environment-selected model resolves its own entry. |
| Output6000 tokens; legacy2048; developer8192; wire16000/19000 | orchestrator/chat_schema.py, model.py; model-gateway/agent_provider.py, streaming.py, dev_gateway.py; packages/dev_request.py | Justified: latency/cost policy, provider replies and retry-inclusive usage. |
| EUR0.10 ordinary /0.30 attachment or file production, legacy0.05;10 tools;120 seconds | orchestrator/loop.py, model.py, chat_pipeline.py; model-gateway/quality.py | Justified: owner-approved request budgets; estimates reserve before calls, actual usage settles. |
| 3 searches,8 fetches,900 monthly reservations | orchestrator/loop.py, model.py, cache.py | Justified: tool budget and90% provider quota. |
| 100 chat messages;1024 developer messages;16 multipart items;8 attachments | packages/images.py, dev_request.py; orchestrator/chat_schema.py, image_store.py; model-gateway/agent_provider.py | Justified: bounded validation/structured-message overhead independent of text tokens. |
| 4 images;2 MiB/image;4 MiB combined;4096/axis;16000000 pixels;1 frame | packages/images.py, image_upload.py; model-gateway/quality.py | Justified: decompression memory and vision provider envelope; counts/bytes/pixels are separate from token capacity. |
| 2796204 encoded image characters;2796300 URL characters | packages/images.py | Justified: base64 overhead for2 MiB plus format header. |
| 32 MiB chat/vision public and internal HTTP input;800000 other gateway input | orchestrator/chat_api.py; model-gateway/http_gateway.py | Justified memory bounds; M25 removes the inherited800000-byte ceiling for agent chat/stream requests to match the public32MiB envelope. Rejections report measured bytes and threshold. |
| 160000 malformed body bytes;5-second upload read deadline | orchestrator/chat_api.py | Diagnostic classification and slow-upload bound, separate from valid-body limit. |
| 32000 characters retained from invalid question | orchestrator/chat_api.py | Justified: bounded rejection journal; successful questions are retained completely. |
| 16 MiB document;4 MiB extracted text;1000 pages;64 MiB expanded archive;255 filename characters;22370000 encoded characters | orchestrator/documents.py | Justified: bounded extraction/decompression and filesystem filename limits. Failures disclose measured sizes. |
| 8 MiB terminal response;15000 command characters;1000 path characters;16 MiB download | orchestrator/terminal_client.py | Justified: bounded shell output, command encoding and downloadable user artifacts. |
| 131072 evidence bytes;half remaining budget | orchestrator/chat_pipeline.py document_evidence | Justified: invokes complete hierarchical synthesis before a costly follow-up; not silent truncation. |
| 8000 evidence tokens;4000 synthesis tokens;2400/5000 hierarchy windows;800 minimum window | packages/evidence.py; orchestrator/content.py, tools.py, chat_pipeline.py | Justified: context/output budget partition; synthesis retains provenance and disclosure. |
| 32000 raw-preview characters plus32000 excerpt characters;16000/page each;300 title;100 date;5 results | orchestrator/tools.py | Bounded search-provider response and display metadata; full page content separate from snippet. |
| 4000 search/RAG query characters;4096 URL characters;32000 focused-fetch query characters | orchestrator/tools.py, web.py; retrieval/tool.py | Justified: provider/transport envelope and bounded parsing. |
| 2 MiB page;1 MiB extraction output;300000 extraction subprocess response | orchestrator/web.py, tools.py; packages/web_extract.py | Justified: network and subprocess memory containment. |
| 512 calculator characters;128 AST nodes | orchestrator/calculator.py, tools.py | Justified: CPU/memory denial-of-service prevention. |
| 2000 image prompt characters;512 model input tokens;16000 worker body;2800000 result bytes;32-bit seed | packages/imagegen.py; orchestrator/image_tool.py; model-gateway/image_worker.py, imagegen.py | Justified: image model context, bounded encoded output and seed domain. |
| 40 GiB device selection;1024 image sides;4 generation steps | model-gateway/image_worker.py | Justified: GPU placement and fixed PoC generation quality/cost configuration. |
| EUR2/h;EUR30 startup envelope;895-second loading allowance | model-gateway/imagegen.py, image_lifecycle.py | Justified: owner GPU cap and separate bounded model loading. |
| 800000 model/retrieval response bytes;8 million profile stream bytes;1 million legacy stream bytes | orchestrator/model.py, chat_pipeline.py, stream_client.py; model-gateway/generation.py, agent_provider.py, vision.py; retrieval/search.py | Justified: bounded response/stream memory, including JSON event overhead. |
| 128000 answer characters;256000 reasoning characters;32000 legacy answer characters | orchestrator/model.py, vision.py; model-gateway/agent_provider.py, generation.py | Justified: protocol envelope larger than output-token allowance; not an input window. |
| 80 tool-name characters;200 call/model identifier;16000 argument characters;10 calls | orchestrator/model.py; model-gateway/agent_provider.py, streaming.py | Justified: bounded untrusted provider protocol, aligned with ten-tool ledger. |
| 8 offered tools;1 completion choice | model-gateway/agent_provider.py, streaming.py | Justified: implemented tool surface and single-answer API. |
| 240 characters simple routing | model-gateway/agent_provider.py | Heuristic, not rejection; longer requests route to the expert. |
| 3 identical consecutive tool calls | orchestrator/loop.py | Justified: terminate repetitive loops within hard budgets. |
| 32 candidates;8 passages;32000 question/chunk characters;128000 batch characters;1024 embedding dimensions | retrieval/search.py, store.py, answer.py; model-gateway/gateway_cpu.py, generation.py; orchestrator/chat_pipeline.py | Justified: bounded CPU reranking/embedding batches and retrieval context, distinct from chat history. |
| 10 MiB files;1 million extracted characters;1 MiB decompressed document | retrieval extraction pipeline | Justified: ingestion memory/decompression envelope; long documents are chunked. |
| 120 project-name;4000 instruction;500 fact/evidence;8 facts/history turns;32000 stored turn characters | retrieval/project_api.py, projects.py; orchestrator/project_chat.py, memory.py | Justified: compact shared memory and bounded project protocol; source documents remain separately retrievable. |
| 160000 project request;800000 project response;32000 project-stream buffer | retrieval/project_api.py; orchestrator/project_ui.py, project_stream.py | Justified: memory protocol/stream accumulation bounds. |
| 64 developer tools/calls;64 name characters;16000 description;65536 arguments;200 call ID;131072 streamed text | packages/dev_request.py; orchestrator/dev_chat.py | Justified: separate developer protocol and bounded assembly memory. |
| 32 metadata entries;1024 metadata value;1048576 input ID | orchestrator/dev_input.py | Justified: bounded developer protocol metadata/history handle. |
| 1048576 developer request/response bytes;2 million provider response bytes | orchestrator/dev_inference.py; model-gateway/dev_gateway.py | Justified: independent developer transport memory envelope. |
| 50000 reserved developer tokens;1048576 reported input tokens | orchestrator/dev_auth.py; model-gateway/dev_gateway.py | Justified: developer quota ledger and bounded usage claims. |
| 43–100 key characters;1 authorization header | orchestrator/dev_auth.py, devapi.py | Justified: supported key format and ambiguous-auth rejection; never reflect key contents. |
| 64 MCP server/tool name;65536 configuration bytes;16384 call/result bytes;100 pending confirmations;1024 confirmation-body bytes | orchestrator/mcp_client.py, mcp_confirmation.py | Justified: containment of configuration, external tool messages and confirmation memory. |
| 100 eval messages;32000 eval input bytes;EUR0.05;120 seconds;800000 response bytes;2048 output tokens | model-gateway/eval_provider.py | Justified: independent evaluation cost and transport budgets. |

Binary format identifiers (64-character SHA256, UUIDs), minimum nonempty strings,
zero/nonnegative counts, finite numeric checks and supported enumeration cardinality
are schema invariants, not arbitrary content ceilings. Model buffers, network chunk
sizes and batching sizes are implementation parameters unless they reject/truncate
content. Classification heuristics do not reject inputs.

Boundary regressions check safe measured quantities for schema, transport, model,
image and money refusals. Malformed values without a meaningful size or numerical
bound remain typed protocol/validation errors; they are not size-limit refusals.

M25 correction: the public harness aggregate262144-token ledger is removed; it
counted repeated history across tool turns against one context window. Each gateway
call still enforces the configured model window, and independent cost/time/tool
limits bound cumulative work. Legacy explicitly token-limited callers are unchanged.
Recovery attempts retain a3000-token ceiling within the6000-token public maximum.
Web redirects: at most5 HTTP attempts (4 followed redirects), transport memory and
latency bound. MCP transport:262144 total response bytes and31 notifications before
a reply. These are justified transport/loop bounds; numerical errors disclose them.

MCP overlong lines now preserve the stream reader's measured byte count and
262144-byte threshold even when the delimiter itself exceeds the buffer. EOF is
reported as a protocol interruption, not incorrectly as a size limit.

Additional inventory from the production scan:

| Value | Enforcement | Reason / classification |
|---|---|---|
| 512 ranking tokens | model-gateway/gateway_cpu.py CrossEncoder | Justified fixed reranker input capacity; ranking is a relevance heuristic, not a claim to have read the whole source. Full selected chunk text remains available. |
| 500 query characters | orchestrator/search_policy.py | Justified bounded automatically seeded query, distinct from the original question retained by the harness. No conversation rejection. |
| 16000 retrieval-tool input bytes | retrieval/tool.py | Justified CLI envelope around the4000-character query; reads16001 to detect excess. |
| 512 gateway error-body bytes | orchestrator/model.py; model-gateway/imagegen.py | Bounded parsing of untrusted error diagnostics; never source content. |
| 40 profile characters | model-gateway/agent_provider.py | Justified public alias protocol envelope, then validated against configured choices. |
| 200 developer call identity characters;64 call indexes | orchestrator/dev_chat.py | Justified streamed protocol assembly limits; preserve consistent identifiers. |

Evaluation input count/bytes and output transport size now raise measured limit
errors; the public harness also reports measured completion tokens against its
configured allowance including recovery. Existing provider budgets are unchanged.

Current M25 continuation: public profile transports retry transient HTTP502/503/504,
invalid JSON and empty completions at most3 times after2/5/15seconds. Each repeated
call reserves its original maximum against the same remaining monetary allowance;
unknown charges are retained. The original deadline also applies. Once deltas have
been emitted, failure propagates rather than duplicating visible output. Legacy serverless and evaluation coverage is detailed below.
For empty streaming completions with validated usage, retried attempts settle to
the measured input/output cost before the next full reservation. Usage exceeding
the original token reservation prevents retry. Without validated usage, the full
reservation remains charged. The outer gateway also settles the final failed attempt to validated usage;
unknown usage retains the full reservation. Usage above the reserved input/output
bounds fails before recovery. This changes no per-request ceiling.
Gateway aggregate completion accounting accepts up to48000 tokens, with at most
36000 separately identified retry-reservation tokens. The harness still checks
non-retry completion against its requested maximum plus3000 recovery tokens;
individual provider output remains at most6000. These are accounting envelopes,
not an increase in the per-request monetary budget or model context.

Search preview allocation divides the remaining monetary allowance by the
configured input price and JSON escaping overhead, retaining half the budget for
output/other context. An inherited multiplier for ten hypothetical future turns
was removed to implement the owner's per-step reservation decision. Each source reports measured
preview bytes and its derived threshold when reduced. Complete cached sources and
fetch links remain available; reduced extracts are not described as full readings.
A monetary gateway rejection now preserves cost_budget, measured microEUR and the
remaining limit instead of being mislabeled request_size_exceeded.

Owner decision2026-09-21: file-producing requests now share the attachment ceiling
of EUR0.30 across inference and tools; ordinary requests retain EUR0.10. The
server derives eligibility from the requested action/artifact, never a client budget.
Gateway and orchestrator independently validate the ceiling, then deduct actual
prior usage before each step. A bounded recovery margin remains available where
possible; no full sequence of future tool costs is reserved in advance.

Evaluation transport now retries HTTP502/503/504, malformed wire JSON and empty
streams at2/5/15seconds, with one120second deadline and the original EUR0.05 cap.
Unknown failed-call charges retain their full reservation in the final cost;
a retry that cannot fit is refused before I/O. Legacy serverless retries are detailed below. Streaming failures disclose EUR0.30 for file requests and
EUR0.10 for ordinary requests, matching the server-derived allowance.


Further measured boundaries: legacy non-stream completion2048 tokens/32000
characters; provider SSE schema lengths; source citation response800000 bytes;
agent timeout120000 milliseconds. Their safe numeric diagnostics survive JSON/SSE
responses rather than collapsing to an unqualified provider_error. Budget-unit
errors never use HTTP413. The262144-token document hierarchy trigger in
orchestrator/document_chat.py selects synthesis; it is not a rejection ceiling.

Numeric monetary diagnostics now include requested profile allowance versus its
server-derived ceiling, image rewrite reservation versus remaining image allowance,
and configured GPU hourly rate versus EUR2/h. Image rewrite deadline exhaustion
reports elapsed and allowed milliseconds, rather than mislabeling time as money.
Malformed/nonfinite monetary inputs remain validation errors without echoing input.

Image dispatch keeps a EUR0.05 sub-budget for rewriting plus GPU inference inside
its enclosing EUR0.30 file request. Dispatch separately reports monetary microEUR
and timeout milliseconds (120000 maximum); GPU startup reports proposed cumulative
reservation versus30000000 microEUR. Startup wait validation reports895000
milliseconds. These existing limits remain unchanged; only diagnostics changed.

Legacy serverless complete/stream now use the same bounded transient retry ledger,
inside the already reserved primary/fallback sum (never above EUR0.05). Unknown
charges reduce the fallback allowance; inability to afford another call produces
a measured cost refusal before I/O. Returned costs include those reservations.
The public client validates reported legacy cost against its own reservation.

The search snippet comes from the provider's query-relevant
excerpt, within the per-page ceiling rather than an arbitrary300-character prefix. Raw preview and complete
cached page limits are unchanged. Source: Tavily's official parameter guidance,
https://help.tavily.com/articles/7879881576-optimizing-your-query-parameters.

Monetary gateway refusals use HTTP429 with measured microEUR, never HTTP413.
The client retains reservation/remaining/request-ceiling figures; byte refusals
keep HTTP413. Non-stream schema length errors also retain measured bounds.

Search snippets retain up to the shared per-page preview ceiling (min16000,32000/result count characters), replacing the leftover300-character cut. Full page text remains in cache and web_fetch remains available after discovery. This bounds model context while preserving the provider’s relevant excerpt.

Tavily advanced retrieval charges2 credits for every search, against the unchanged900-credit local monthly stop and existing2-credit per-tool monetary reservation. This replaces basic discovery that returned insufficient contextual evidence.

Cached search-provider pages use the same4000-token hierarchical extraction as
pages downloaded directly. The complete source remains behind its cache handle;
selected passages, source size and consultation provenance accompany the synthesis.
This closes a cache-path bypass without lowering the existing evidence allowance.

Exhausted public-profile retries also settle the last empty completion at validated
provider usage before reserving a fallback. Missing usage retains the full reserved
charge; usage exceeding the token reservation stops before another paid call.
Request ceilings remain unchanged.

When the tool quota leaves no available tools after acquired evidence, the gateway
enters the same explicit answer-only mode as monetary finalization. It preserves
all evidence and the current question, and asks for the final answer without
another tool call. This changes neither the ten-tool cap nor any monetary limit.
