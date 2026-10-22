# Capability strategy regression

Owner defect: 2026-10-21. Explicit image retrieval and QR requests must produce
inline local images through executed tools. The six fixed cases include the
verbatim owner request, French and English variants, cats, Art Deco and Eiffel.

Run from the project root, with the existing virtualenv:

```
export ATLAS_PORT_BASE=6900
./.venv/bin/python -m evals.capability.harness --output BRAIN/capability-strategy/offline
./.venv/bin/python -m evals.capability.harness --live --screenshots --output BRAIN/capability-strategy/live
```

Offline mode patches only model/provider boundaries: a scripted model issues tool
calls, consumes their results and returns Markdown through the real chat pipeline.
The real runtime, image validation and disk store execute. Photo fixtures are
synthetic colored squares; offline mode does not measure semantic search relevance.
Unit tests separately exercise deterministic routing, public DNS/redirect refusal,
byte/type/pixel limits, quota failures and source association.

Live mode uses the running public HTTP stack, captures SSE tool metadata and actual
answer images, and optionally renders those captured outputs into screenshots.
These are evidence galleries, not screenshots of the chat application UI. Inspect
them manually for relevance and visible defects. Run cases serially and respect
owner provider balances and call limits; `--case ID` restricts a live batch. The
harness does not reset quotas, provision a stack, generate images with a model,
or replay provider recordings. QR routing requires no chat-model calls.

Criteria: no first-sentence inability claim, the specified minimum of distinct
local `/images/` Markdown references, executed tools, distinct image bytes and
valid PNG dimensions. If pyzbar and its native decoder are already installed,
QR checks require decoded payloads and compare an explicitly requested payload;
otherwise the result explicitly records the permitted PNG/size fallback.
No dependency is installed at test time. Vendor Segno is the encoder, not a decoder.

Tool schemas/descriptions are Python-owned. Image search respects the configured
provider (Tavily includes images in its normal search; otherwise SerpAPI Images),
request, daily/global and monthly quotas, and reports provider status on failures.
Source pages must be associated by the provider, not inferred from image domains.
Credits link only a successfully fetched page or the fetched original image.
