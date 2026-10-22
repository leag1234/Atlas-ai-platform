# Trial search

The trial uses the pinned SearXNG sidecar without purchased search credits.
`ATLAS_SEARXNG_URL` overrides its base URL (Compose default `http://searxng:8080`).
The standalone runtime uses the port-base-derived local SearXNG URL.

Search language comes from conversation history and current user prose; short
follow-ups inherit history. Ambiguous text falls back to the UI locale, then `en`.
Search supports `en`, `de`, `es`, `fr`, `it`, `nl`; it never sends `auto` or `all`.
Dutch search is supported independently of the existing five text UI translations.
Model-generated engine, language and timeout operators in query text are quoted.

The default web order is DuckDuckGo, Brave, Qwant, Startpage, Bing, Google.
Ecosia has no engine in the pinned 2026.10.17 image and is omitted. Wikipedia
runs concurrently in the query language and contributes up to one additional
result, even when the requested number of web results is already available.
Brave is used when DuckDuckGo fails, is cooling down, or returns too few results.
The remaining engines are tried individually when the first two cannot meet the
minimum. The first fallback with useful results stops the fallback chain.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ATLAS_SEARXNG_ENGINES` | `duckduckgo,brave,qwant,startpage,bing,google` | Ordered, unique subset of supported web engines; first two are primary tiers |
| `ATLAS_SEARXNG_MIN_RESULTS` | `0` | Minimum web results; 0 uses requested count, otherwise 1–5 |
| `ATLAS_SEARXNG_QUARANTINE_SECONDS` | `900` | Quarantine after three consecutive block signals, range 900–3600 |
| `ATLAS_SEARXNG_PROBE_SECONDS` | `60` | Minimum spacing for request-triggered health probes, at least 60 |
| `ATLAS_SEARXNG_ENGINE_SECONDS` | `6` | Minimum call spacing for DuckDuckGo, Brave and Wikipedia, at least 6 |
| `ATLAS_SEARXNG_FALLBACK_SECONDS` | `30` | Minimum spacing for each other engine, at least 30 and no less than primary spacing |

CAPTCHA, HTTP 403/429 and SearXNG engine suspension produce block signals.
There is no immediate retry. Retry-After seconds and HTTP dates are honored on
responses from the configured endpoint. SearXNG's JSON engine failures do not
forward upstream Retry-After headers; those receive the conservative cooldown.
Each engine needs two successful probes, spaced apart after quarantine expires,
before its circuit reopens. No background probes run without user searches.
Timeouts, empty results and malformed responses cannot count as successful probes.

The single-process trial shares a rolling limit of ten uncached logical searches
per minute, at most three searches and three engine HTTP requests concurrently,
per-engine circuits and single-flight deduplication. Multiple replicas would need
a shared coordinator before scaling; each process currently owns its limits.
The existing disk cache remains: complete results last one hour, partial results
one minute, and empty/blocked/timeout outcomes are not cached. Endpoint, effective
language, query, count, news flag and policy are part of cache identity.

Tool results include `outcome`, `language`, `results` and per-engine evidence.
Outcomes are `ok`, `upstream_blocked`, `upstream_timeout`, `empty_results`, or
`partial_results`. Available sources survive partial failures. The chat pipeline
stops before unsupported synthesis when no result is available, names the outcome,
and never turns empty search results into a claim that no information exists.
Partial answers and streaming tool status explicitly disclose limited results.

Do not combine `categories` with `engines`: the pinned SearXNG adapter adds all
category engines to the explicit list. Wikipedia must use `display_type: [list]`
to appear in JSON results. Retries are disabled in the sidecar. Engine definitions
being present does not guarantee that public engines will accept this server's IP.

Sources checked 2026-10-22: pinned image engine registry, `searx/query.py`,
`searx/webadapter.py`, and the official
[search API](https://docs.searxng.org/dev/search_api.html) and
[Wikipedia engine documentation](https://docs.searxng.org/dev/engines/online/wikipedia.html).
