# Trial search

The trial uses the pinned SearXNG sidecar without purchased search credits.
`ATLAS_SEARXNG_URL` overrides its base URL (Compose default `http://searxng:8080`).
The standalone runtime uses the port-base-derived local SearXNG URL.

Search language comes from conversation history and current user prose; short
follow-ups inherit history. Ambiguous text falls back to the UI locale, then `en`.
Search supports `en`, `de`, `es`, `fr`, `it`, `nl`; it never sends `auto` or `all`.
Dutch search is supported independently of the existing five text UI translations.
Model-generated engine, language and timeout operators in query text are quoted.

The default web order is Startpage, DuckDuckGo, Mojeek, Seznam, Qwant, Brave,
Google. Bing is accepted in `ATLAS_SEARXNG_ENGINES` but is not in the default
order: in the 2026-11-14 measurement about half of its pages were unrelated to
the query. Ecosia has no engine in the pinned 2026.10.17 image and is omitted.
Wikipedia runs concurrently in the query language and contributes up to one
additional result, even when the requested number of web results is already
available. The second engine is used when the first fails, is cooling down, or
returns too few results. The remaining engines are tried individually when the
first two cannot meet the minimum. The first fallback with useful results stops
the fallback chain. An engine skipped for local pacing (cooldown or quarantine)
does not make a search partial when another engine answered.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ATLAS_SEARXNG_ENGINES` | `startpage,duckduckgo,mojeek,seznam,qwant,brave,google` | Ordered, unique subset of supported web engines (the default ones and `bing`); first two are primary tiers |
| `ATLAS_SEARXNG_MIN_RESULTS` | `0` | Minimum web results; 0 uses requested count, otherwise 1–5 |
| `ATLAS_SEARXNG_QUARANTINE_SECONDS` | `900` | Quarantine after three consecutive block signals, range 900–3600 |
| `ATLAS_SEARXNG_PROBE_SECONDS` | `60` | Minimum spacing for request-triggered health probes, at least 60 |
| `ATLAS_SEARXNG_ENGINE_SECONDS` | `3` | Minimum call spacing for the first two engines and Wikipedia, at least 3 |
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
category engines to the explicit list. This happened from 2026-10-24 to
2026-11-14: each engine call reached every enabled web engine, and the volume got
this server's IP refused by most of them. The sidecar settings now disable every
engine, so a category search, or an engine name that SearXNG did not load,
reaches no engine; an explicit `engines=` request still selects a disabled
engine. Every engine Atlas names must be in `keep_only` and must not be inactive
(a unit test checks it). Startpage and Mojeek are inactive upstream because they
answer with a proof-of-work challenge; the pinned engines solve it.

A request with `time_range` skips every engine without time-range support
(Bing, Brave, Qwant, Seznam in the pinned image) without an error. News searches
send `time_range=month` only to DuckDuckGo, Google, Startpage and Mojeek.

Wikipedia must use `display_type: [list]` to appear in JSON results. The engine
looks up a page title, so it adds a result mainly for short entity queries
(1 of 40 measured queries). Retries are disabled in the sidecar. Engine
definitions being present does not guarantee that public engines will accept
this server's IP.

## Engine measurement (2026-11-14)

40 queries (20 French, 20 English: news, prices, local businesses, people,
technical), one request per engine every 30 s from the VM's Docker bridge, no
local suspension, pinned 2026.10.17 image:

| Engine | Answered | Notes |
| --- | --- | --- |
| Startpage | 40/40 | Google-sourced pages, relevant; Anubis challenge solved by the engine |
| DuckDuckGo | 39/40 | relevant, fastest (median 0.7 s); one CAPTCHA |
| Mojeek | 32/40 | independent index; median 2.0 s; the rest refused or empty |
| Seznam | 40/40 | independent index, Czech bias on French and English queries |
| Bing | 40/40 | about half of the pages unrelated to the query |
| PrivacyWall, Mwmbl | 40/40, 38/40 | not selected: Bing-sourced with a reported data-sharing concern; very small index |
| Qwant, Google, Brave | 0/40 | access denied, access denied, too many requests |
| Yahoo, Yep, GMX, Fireball, Tusksearch, Resulthunter | 0/40 | refused |

The same engines over the VM's IPv6 address gave the same refusals, so IPv6
egress is not a remedy. The query set and per-engine numbers are in the pull
request that introduced this section.

Sources checked 2026-10-22 and again 2026-11-14: pinned image engine registry,
`searx/query.py`, `searx/webadapter.py` (`parse_generic`),
`searx/search/processors/abstract.py` (`get_params` skips on time range), the
[search settings](https://docs.searxng.org/admin/settings/settings_search.html)
(default suspensions of one hour to fifteen days), the
[outgoing settings](https://docs.searxng.org/admin/settings/settings_outgoing.html),
the [limiter](https://docs.searxng.org/admin/searx.limiter.html) (bot filtering
for clients of a public instance, not needed for this loopback sidecar),
[Startpage proof-of-work solver](https://github.com/searxng/searxng/pull/6669), and the official
[search API](https://docs.searxng.org/dev/search_api.html) and
[Wikipedia engine documentation](https://docs.searxng.org/dev/engines/online/wikipedia.html).
