# Specialized public sources before a general web search

Roadmap T8: what a precise official source answers is fetched by code from that
source. The dominant measured defect is invented facts about named entities (dates,
people, places); a general search plus the model's memory is the weakest way to
answer them, and the general engines behind SearXNG block a datacenter address under
load. Four chat tools read precise public sources first. The model still decides, and
`web_search` stays available on the same turns.

## Tools

Declared in code (`services/orchestrator/source_tools.py`), not in `prompts/`.

| Tool | Module | Source | What it answers |
|---|---|---|---|
| `encyclopedia` | `encyclopedia.py` (+ `kiwix.py`) | Wikipedia (Action API, edition of the user's language, English as fallback) and Wikidata (`wbgetentities`) | dates of birth and death with their precision, places, founding date, founders, office holders with start and end dates, latest population with its date, headquarters, authors and creators; the article's introduction |
| `news_headlines` | `news_feeds.py` | public RSS and Atom feeds of 27 outlets (French and European first, public broadcasters preferred) | latest headlines on a topic or overall: title, outlet, time, link, short description |
| `eu_statistics` | `eu_statistics.py` | Eurostat dissemination API (JSON-stat), 12 curated indicators | population, unemployment (all, under 25), HICP inflation, GDP, real growth, GDP per inhabitant, public debt and deficit, minimum wage, life expectancy, fertility, for EU, euro area and countries |
| `french_commune` | `fr_communes.py` | national register of elected officials (RNE), mayors file (French Ministry of the Interior, data.gouv.fr) and the Découpage administratif API (geo.api.gouv.fr) | current mayor with the start of the function, INSEE municipal population, INSEE code, department, postal codes; homonyms listed |

All outbound requests go through `reference_fetch.py`: HTTPS only, a fixed list of
hosts, no redirects, a size ceiling per answer, a per-host concurrency limit (two for
Wikimedia), a descriptive User-Agent carrying the project URL and no personal data,
and an in-memory cache (Wikipedia and Wikidata one day, feeds ten minutes, Eurostat six
hours, whole datasets one day).

## Sources, terms and limits

| Source | Account | Terms | Limits applied |
|---|---|---|---|
| Wikipedia Action API | none | text CC BY-SA 4.0; [API etiquette](https://www.mediawiki.org/wiki/API:Etiquette) and [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy) | at most two parallel requests, one-day cache, gzip |
| Wikidata `wbgetentities` | none | data CC0 | same, plus `maxlag=5`; a `maxlag` refusal is reported as rate limited |
| Eurostat API | none | [reuse with attribution](https://ec.europa.eu/eurostat/about-us/policies/copyright) | six-hour cache; the dataset page is cited |
| data.gouv.fr (RNE) | none | Licence Ouverte 2.0 | whole file (4.3 MB) once a day; the file's birth dates and occupations are never returned |
| geo.api.gouv.fr | none | Licence Ouverte 2.0 | whole commune list (4.2 MB) once a day |
| News feeds | none | publisher terms, which differ (see below) | ten-minute cache per feed; headline, date, link, at most 280 characters of description, outlet named |

News feeds: every listed feed is public and was reachable on 2026-11-15. Reuse terms
are not uniform and several publishers do not state them on the feed. The tool shows
what a feed reader shows (headline, date, link, short description, outlet), never the
article. Before a commercial launch the list needs a terms review by the owner; the
operator can narrow it with `ATLAS_NEWS_OUTLETS` (comma list of outlet ids).

## Routing

`source_tools.relevant` reads the question (lower case, accents removed, five interface
languages, tolerant of typos and of short follow-ups that inherit the previous
question's subject) and returns the families it is about. Only those tools are
declared, so every recorded provider request (chat, journeys, M21 to M25 cassettes)
is unchanged: no recorded chat question matches. A request that asks for a web search
explicitly keeps the web tools only.

When the mandatory current-facts search would run (`search_policy.required_research`)
and the question is about news, the turn reads the news feeds first instead of a
general search; the model may search the web after reading them.

## Truthfulness (D7)

Every URL a result names was fetched for the turn: the Wikipedia page and the Wikidata
item, the Eurostat dataset page, the data.gouv.fr dataset page and the geo.api.gouv.fr
record, each feed read and each headline link. The chat adds them to the turn's
retrieved sources, so the answer may cite them and the citation filter keeps them. A
headline link was read from the feed, not opened: the tool says so and the answer
attributes the headline to its outlet. No source invents a value: an unreachable,
malformed or empty answer is a named error (`source_unavailable`,
`no_encyclopedia_entry`, `news_feeds_unavailable`...), and the descriptions send the
model to `web_search` for what is missing.

## Privacy levels

| Tool | Capability | Standard | Enhanced | Strict |
|---|---|---|---|---|
| `encyclopedia`, live API | `reference_lookup` (carries the subject) | yes | refused, like web search | refused |
| `encyclopedia`, local mirror | none (Atlas-operated server and file) | when the live API fails or times out (dated copy; facts from the local extract) | yes, facts from the local extract | yes, facts from the local extract |

Without a configured mirror, the `encyclopedia` tool is not offered in enhanced and
strict turns, and a call is refused with the level's code.
| `news_headlines` | `news_feeds` (fixed URLs, no request content) | yes | yes, like the currency rate feed | refused |
| `eu_statistics` | `public_statistics` (carries the country and indicator) | yes | refused | refused |
| `french_commune` | `public_datasets` (whole files, searched locally) | yes | yes | refused |

`reference_fetch.py` is classified third party in `tests/test_strict_privacy_egress.py`
and calls `require_shared` before any request; `kiwix.py` is classified internal.

## Local Wikipedia mirror

The encyclopedia has two backends:

- the live Wikipedia and Wikidata APIs;
- a local mirror: a [kiwix-serve](https://github.com/kiwix/kiwix-tools) instance run by
  Atlas (`ATLAS_KIWIX_URL`) with one ZIM book per language (`ATLAS_KIWIX_BOOKS`). A
  book is named either by its dated name (`fr=wikipedia_fr_all_mini_2026-11`) or by its
  undated family (`fr=wikipedia_fr_all_mini`): a family is resolved to the newest book
  of that family listed in kiwix-serve's own catalog (`/catalog/v2/entries`, read again
  after five minutes or after a failed lookup). A family the catalog does not list, or
  an unreadable catalog, makes the mirror unavailable for that language; nothing
  outside the mirror is asked. Instances use family names, so a refreshed book is
  picked up without a settings change. The article is found with kiwix-serve's title index first (`/suggest`), then its
  full-text search when no title equals the query: on the `de` mini book, full-text
  search for "Albert Einstein" ranked schools and prizes named after him and did not
  return his article in its first ten results. The title as written comes first
  (`München` before the redirect `Munchen`), disambiguation pages and categories
  last, and the mirror's redirect entries (`Einstein`, `Goethe`) are followed to the
  article. Its introduction is read from the local page; the answer cites the public
  Wikipedia address and names the local book, whose date (`copy_date`, from the served
  book's name, a family being resolved first) gives the copy's age. Structured
  Wikidata facts come from the local Wikidata extract when one is configured
  (`ATLAS_WIKIDATA_LOCAL`, see "Local Wikidata facts" below), at every level; without
  it, from the Wikidata API when the request's level allows it, and they are left out
  otherwise (summary only).

The backend is chosen per request (`encyclopedia.source_plan`), from the request's
privacy level and the setup (`packages.privacy.local_encyclopedia`: a URL and one
well-formed book at least):

| Setup | Standard | Enhanced, strict |
|---|---|---|
| mirror configured | live API; on an outage, HTTP 429, unreadable answer or timeout, the local copy, with `live_error` and a note naming the copy's date | local copy |
| no mirror | live API | tool not offered; a call is refused |
| `ATLAS_ENCYCLOPEDIA_BACKEND=local` (offline instance) | local copy | local copy |

Standard keeps the live API first because Wikidata facts are fresh (current office
holders, latest figures) while a `mini` book is months old. When a mirror is
configured, the live attempt gets the tool's time budget minus three seconds, kept for
the local copy. "No article" from the live API is an answer, not an outage: the copy is
not read then. A fallback answer carries the local extract's facts (none without an
extract), and its note asks the model to say that the answer comes from a copy of that
date.

### Survey (2026-11-15)

Kiwix ZIM files of Wikipedia (`https://download.kiwix.org/zim/wikipedia/`, sizes from
the server's Content-Length). `mini`: introduction and infobox of every article;
`nopic`: full text without images; `maxi`: full text with images.

| Language | mini | nopic | maxi |
|---|---|---|---|
| fr | 5.67 GB (2026-11) | 12.91 GB (2026-05) | 55.44 GB (2026-05) |
| en | 14.39 GB (2026-09) | 52.69 GB (2026-06) | 127.42 GB (2026-08) |
| de | 4.69 GB (2026-10) | 18.59 GB (2026-10) | 48.63 GB (2026-10) |
| es | 4.11 GB (2026-10) | 13.30 GB (2026-10) | 36.52 GB (2026-10) |
| it | 3.54 GB (2026-11) | 9.03 GB (2026-08) | 31.79 GB (2026-08) |
| total | 32.4 GB | 106.5 GB | 299.8 GB |

Cadence: a new file per language every three to six months (dates in the listing).
kiwix-serve is GPL-3.0, published as `ghcr.io/kiwix/kiwix-serve` (3.8.2), and serves
search (`/search?books.name=…&pattern=…&format=xml`), suggestions and pages from the
ZIM's own full-text index.

Measured on the build VM (2026-11-15) with the `wikipedia_fr_top_mini_2026-10` book
(153 MB, SHA-256 checked against the published `.sha256`, 50 000 best French articles
plus redirects): image 92.8 MB; memory 4.5 MiB idle and 153 MiB after a few dozen
queries; full-text search 13 to 24 ms, article page 2 ms; the `encyclopedia` tool on
the local backend answered in 20 to 30 ms in strict mode (summary only) and in 0.8 to
1.1 s in standard mode, the time of the uncached Wikidata facts. Search ranks by text:
a museum named after a person came before the person's article, so the backend puts
the article titled as the query first. The download ran at about 200 KB/s from the
Kiwix redirector (153 MB in 774 s): the full `mini` set needs a faster mirror or the
published torrents.

Wikidata (`https://dumps.wikimedia.org/wikidatawiki/entities/`, weekly):
`latest-truthy.nt.gz` 71.9 GB and `.bz2` 43.7 GB (2026-11-13), `latest-all.json.gz`
157.0 GB and `.bz2` 103.8 GB (2026-11-11). Existing slimming tools (state on GitHub,
2026-11-15): [wikibase-dump-filter](https://github.com/maxlath/wikibase-dump-filter)
(streams the JSON dump, keeps chosen claims, languages and sitelinks; archived in
2026, no licence detected by GitHub), [wdumper](https://github.com/bennofs/wdumper)
(MIT, custom filtered RDF dumps, last change 2022),
[simple-wikidata-db](https://github.com/neelguha/simple-wikidata-db) (splits the dump
into tables, active, no licence detected by GitHub). None is a maintained, clearly
licensed dependency. The extract was built on 2026-11-16 from the JSON dump, not the
truthy one, which lacks the qualifiers the facts need: see "Local Wikidata facts".

Wikipedia abstract dumps (`<lang>wiki-latest-abstract.xml.gz`) are no longer published
(HTTP 404 on 2026-11-15); the Wikimedia Enterprise HTML snapshots on dumps.wikimedia.org
stop in March 2025. The `mini` ZIM is the maintained source of introductions.

### Recommended setup

- Books: `mini` for the five languages (32.4 GB) gives introductions and infobox text,
  which is what the tool returns; `nopic` (106.5 GB) only if full articles are wanted
  later. Keep the previous file during a refresh: about 2 x 32 GB for `mini`.
- Facts in strict mode: the local Wikidata extract (next section), built from the JSON
  dump streamed through a filter, stored as SQLite next to the books.
- Refresh: a monthly job on the host (outside the repository; day 3 at 04:10 UTC on
  the build VM) reads the Kiwix listing for each language served, one language at a
  time. When a newer `wikipedia_<lang>_all_mini_YYYY-MM.zim` is published, it checks
  free space (new size plus a margin, never above 85 % of the volume), downloads it
  with parallel HTTP ranges, checks the published `.sha256`, restarts kiwix-serve with
  both books, waits until the catalog lists the new book and a title suggestion
  answers, then deletes the old file and restarts kiwix-serve again to release it.
  `ATLAS_KIWIX_BOOKS` holds family names, so no setting changes; the old file is kept
  while any rendered instance file still names it by its dated name. Any failure keeps
  the old file and writes one alert line.

## Local Wikidata facts

`services/orchestrator/wikidata_local.py` reads a SQLite extract of Wikidata
(`ATLAS_WIKIDATA_LOCAL`, read-only, opened per lookup, no network). The local backend
looks up the article it found on the mirror by its title (`frwiki`, `Lyon`) and adds the
same fields as the live path: `facts` (the `encyclopedia.PROPERTIES` keys, item
references resolved to labels in the user's language, then `mul`, then English),
`description`, `wikidata_url`, `wikidata_modified`, plus `facts_date` (the dump date)
and a note saying that an office taken or a figure published after that date is
missing. An unset or missing file, or an article the extract lacks, gives the previous
answer (summary only at a level that refuses the API); an unreadable file is logged
and treated the same way.

Dump choice: the facts need qualifiers (start and end of an office, point in time of a
population) and every non-deprecated statement (office holders, `latest` population),
not only the best-ranked one. The truthy N-Triples dump has neither, so the extract
is built from the JSON dump (`wikidata-YYYYMMDD-all.json.gz`, 157 GB), whose entities
have the shape of `wbgetentities`: `tools/wikidata_extract.py` runs
`encyclopedia.raw_facts` itself on each entity, so local and live facts cannot drift
apart (`tests/test_wikidata_local.py` compares them on one fixture).

Tools surveyed (2026-11-16, GitHub state): wikibase-dump-filter (archived, no licence
detected, Node.js), wdumper (MIT, RDF output, last change 2022),
[simple-wikidata-db](https://github.com/neelguha/simple-wikidata-db) (no licence
detected, needs the dump on disk and writes every claim as JSON lines),
[qwikidata](https://github.com/kensho-technologies/qwikidata) (Apache-2.0, a dump
iterator over a local file, last change 2023), [wd2sql](https://github.com/p-e-w/wd2sql)
(GPL-3.0, whole dump to SQLite, last change 2023), [KGTK](https://github.com/usc-isi-i2/kgtk)
(MIT, active, converts the whole dump to edge files, heavy on memory and disk),
[QLever](https://github.com/ad-freiburg/qlever) (Apache-2.0, active, a full SPARQL
engine). None streams the dump without storing it, and none computes our fact shape:
the extractor is a single-pass filter of about 400 lines (standard library, `orjson`
when installed for speed).

Content: every item with a main-namespace article on fr, en, de, es or it Wikipedia
(categories, templates and other namespaces left out), with its facts (item
references kept as Q identifiers), descriptions in the five languages and modification
date; labels in the five languages plus `mul` for those items and for every item their
facts name (a place, an office, an occupation, a unit), whether or not it has an
article; article titles by edition, one item per title (the dump is written while
Wikidata is edited, so a title moved between two items can appear under both: the item
modified last keeps it). Tables `entity(qid)`, `label(qid)`, `sitelink(site, title)`
(indexed) and `meta` (dump date, source, counts). Names shared by all languages are
often stored under `mul` only (Douglas Adams has no `en` label): the reader falls back
to `mul`, then English, like the live path.

First build (dump of 2026-11-09, build VM, 4 vCPU, 16 GB): the 157 GB dump (about
1.86 TB of JSON, 122.2 million items) streamed in 96 minutes, about 27 MB/s compressed
and 320 MB/s decompressed; 98 minutes for the whole build including the labels and the
title index. Result: 11,014,302 items with an article, 17,717,518 titles, 11,227,578
label rows (213,276 of them for items named by facts without an article; 14,250 named
items have no label in the five languages or `mul` and stay as Q identifiers), 39
duplicate title rows dropped; **5.73 GB** of SQLite. Peak memory of the extractor
1.73 GB (664 MB while streaming, the rest in the final label pass). Disk at most about
10.6 GB on the data volume (the extract, 3.3 GB of compressed pending labels, at most
1.6 GB of range chunks; the old extract counts once more on a refresh). In strict mode
the tool answered in 80 to 210 ms per lookup once the mirror was warm.

Live check (2026-11-16, in-process): in strict mode, with every third-party request
trapped, Lyon, Montréal, Google, Airbus, Bordeaux, Munich, Victor Hugo and Marie Curie
(French and English) returned facts from the extract (`facts_date` 2026-11-09) with the
Wikipedia address. Wikidata has no founding date for Lyon (Q456 has no `inception`):
the mirror's articles answered that one (Lugdunum, 43 BC). Eight questions on the
standard model with the local backend forced cost 0.063 EUR. The dates, founders and
populations taken from the extract were right; the Airbus answer added founding members
beyond the sources, partly wrong (DASA did not exist in 1970), and the Munich answer
showed the Wikidata figure (1,505,036 at 2025-12-31) next to a different figure of the
article and said they disagree.

Refresh: a monthly job on the host (outside the repository, day 5 at 03:00 UTC, after
the books on day 3) finds the newest complete JSON dump on dumps.wikimedia.org and,
when it is newer than the extract, streams it with parallel byte ranges (three
connections to a mirror, three to dumps.wikimedia.org, its limit; one connection ran at
4 to 20 MB/s) into pigz and the extractor, never storing the dump, into a new file. It
checks the md5 of the stream against the published md5sums, the dump's closing
bracket, SQLite `quick_check`, an entity count within 5 % of the previous extract and
sample lookups, then renames the new file over the old one (readers open the file per
lookup). Any failure keeps the old extract and writes one alert line. A failure while
streaming starts again from the beginning of the dump; a failure after the whole dump
was read (extractor exit 4) keeps the stream's output, which `--finish-only` completes.

## Accounts and decisions needed

- Légifrance (law articles): the official API is on PISTE
  (https://piste.gouv.fr), OAuth2 client credentials, free after creating a PISTE
  account, an application, and subscribing it to the Légifrance API with acceptance of
  its terms. Not integrated: no account was created. Law questions keep web search.
- INSEE (Sirene, statistics catalogue): API keys from the INSEE API portal
  (https://portail-api.insee.fr), free account. Eurostat and geo.api.gouv.fr cover the
  usual figures without an account; the Recherche d'entreprises API
  (recherche-entreprises.api.gouv.fr, no account) ranks homonyms poorly for brand
  names and is not used yet.
- News feed terms: review by the owner before a commercial launch (see above).

## Measurement (2026-11-15)

Twenty questions written as users write them (French and English, typos): birth and
death dates, mayors, populations, founding years, two law articles, today's news on
three topics, five statistics. Direct harness on the real chat pipeline (`process`,
`GatewayModel`, `ChatTools`, in-process gateway, SearXNG search), each question once
per path; "before" is the same code with the specialized tools never offered, which is
the request main sends. Facts checked against the official source (Wikidata, the RNE
file, geo.api.gouv.fr, Eurostat, Légifrance text for the law articles).

| | mistral before | mistral after | qwen before | qwen after |
|---|---|---|---|---|
| web searches (20 questions) | 37 | 15 | 21 | 5 |
| specialized source used | 0 | 18 | 0 | 16 |
| median turn time | 10.9 s | 6.7 s | 7.2 s | 4.3 s |
| cost | 0.175 EUR | 0.161 EUR | 0.261 EUR | 0.166 EUR |
| main fact wrong or not the official figure | 3 | 1 | 2 | 1 |

Before: a mayor of Paris from before the March 2026 elections, a debt ratio from a
national budget plan instead of the Eurostat figure, a population from an aggregator
instead of an official source, an incomplete headquarters. After: the one error on the
mistral path is a law article quoted with the next article's text (no specialized
source covers law yet); on the qwen path, the founding date of the current legal
entity given as the company's founding date (both dates were in the result). Several
answers on both paths still add details from memory around a sourced fact (a party
label, the origin of a brand name); with the mandatory search removed for these
questions the model still called the specialized source every time, and the grounding
check then flagged such an unsourced detail, so the mandatory search is kept.

## Limits and next steps

- The relevance patterns decide whether the model sees the tools; misses found in the
  corpus are added by class of phrasing.
- Questions of the form "who is X?" go through the exact-name person lookup, which
  uses web search; the encyclopedia could feed it next.
- Work turns do not share the chat tool layer.
