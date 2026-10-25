# REQ-NEARBY-01 — owner request, 2026-10-25

Bounded capability, not a milestone. Ordinary chat routing and Work's eight-tool
ceiling remain unchanged. Transport routing is excluded.

## Request and privacy boundary
`location` is optional on ChatRequest. It contains `consent` (`once`, `always`,
`refused`, `denied`), either `position: {latitude, longitude}` or `place: string`,
and optionally an IANA `timezone`. Refusal/denial cannot carry coordinates.
Position is reduced to an approximately 100 m grid before transport and again
on validation. The field is excluded from serialization and repr. It is never
copied into model messages, durable transcript requests, diagnostics or caches.
The browser stores only `always`, keyed by authenticated user id, with revocation
in settings. Each request obtains a fresh fix. Typed places use the same ephemeral
field. The ordinary user question remains ordinary conversation content.

## Tools and output
`geocode(place)` resolves only the place explicitly supplied for this request.
`nearby_places(category, radius=1500, open_now=true)` binds to request-only position;
the model cannot invent a position. Radius is 100–10000 m. Supported categories
include bakery, convenience, pharmacy, supermarket, restaurant, cafe and fuel.
Free text maps to these categories; unsupported categories request clarification.
Results contain 3–5 candidates when available, public business metadata, approximate
straight-line distance, local opening status, next opening/closing time, timezone,
OSM attribution and URLs. No user coordinates leave the tool boundary.
Unknown/unparseable hours remain unknown. Public holidays and overnight rules use
the place's country/timezone. Open candidates rank first, then distance and
completeness; when fewer than three are open, next openings are included.
Web verification is bounded to the highest-ranked uncertain candidates and
reports evidence separately; missing hours are never inferred from snippets.

## Operator settings and dependencies
`ATLAS_NOMINATIM_URL` and `ATLAS_OVERPASS_URL` can select self-hosted services.
`ATLAS_OSM_CONTACT` must identify the responsible operator; requests use an
ATLAS User-Agent with that contact. The public services are user-triggered only,
never autocomplete. A shared file lock limits Nominatim to one request/second
and Overpass to one query/five seconds on this single-host deployment. Multiple
hosts require a shared rate-limiting proxy or self-hosting. Public OSM objects
are cached independently of user locations; request-scoped geocoding is not
persisted. No retries are automatic. Attribution is always visible.

`opening-hours-py==2.1.4` (MIT OR Apache-2.0, maintained Rust/Python bindings,
precompiled wheel) is required: full OSM syntax, embedded holiday calendars and
coordinate-based timezone/country inference. A handwritten parser would silently
misinterpret holidays/overnight rules. Source:
https://github.com/remi-dupre/opening-hours-rs and https://pypi.org/project/opening-hours-py/.
Service policy: https://operations.osmfoundation.org/policies/nominatim/;
https://dev.overpass-api.de/overpass-doc/en/preface/commons.html.

## Acceptance
Focused tests cover consent validation, coordinate minimization/serialization,
opening/closing/overnight/holiday/unknown states, ranking, routing and the observed
citation residue. Separate recorded HTTP cases cover Paris mixed shops, Lyon
pharmacy, rural France, refusal + typed place, abroad and English. Mobile browser
checks cover once, always, denial, settings revocation and identity isolation.
Live calls: <=10 OSM, <=5 search; EUR 0.50 maximum; local checks only.

## Supervisor amendment, 2026-10-25
Candidate sources are sequential: `ATLAS_OVERPASS_URLS` (comma-separated, up to
three endpoints, four-second timeout, one attempt each; empty disables), then
`ATLAS_NOMINATIM_URL` bounded category search (`extratags=1`, `limit=10`), then
configured web search using a public locality only. The legacy single
`ATLAS_OVERPASS_URL` remains supported. All source URLs are configurable for
self-hosting. No automatic retries, autocomplete or bulk queries. The result
and interaction record name the serving source; Nominatim results are a limited
sample, not a complete area inventory. Per-language category terms are checked
against the expected OSM tag. POI caches contain public objects, not a user fix.
The Nominatim rate lock is shared by geocoding and candidate searches.
If no public locality is available for web fallback, ask for a town rather than
send a position to a search provider. Conflicting web closure/holiday evidence
downgrades schedule confidence; it cannot establish new hours.
Work discovers the nearby group through the existing tool-selection mechanism
(five declarations including core tools, below the eight-declaration ceiling),
with normal scoped execution checks and cleanup. Project requests retain the
scoped transcript/lease path. Location links enter request-scoped provenance.
Additional live acquisition ceiling: 10 Nominatim requests, 3 Overpass attempts,
and the original 5 searches; EUR 0.50 total. Local checks are draft evidence,
not milestone or CI completion.

### Open WebUI background-task transport
Open WebUI 0.11.3 sends `user_message` and expects a background-task JSON response.
The browser therefore POSTs consent/location directly to
`/api/atlas/nearby-location`. Atlas returns an unguessable `location_ref`; only
that reference enters the normal host request. The adapter consumes it once
before schema validation. The in-memory store holds at most 256 entries for
120 seconds and never writes them to disk. DELETE cancels an unused reference;
expired/reused references require fresh location input. Cross-origin requests
are rejected and responses use `Cache-Control: no-store`. This single-worker
adapter deployment needs sticky routing or equivalent ephemeral storage before
scaling to multiple workers. Direct Project/API requests keep the excluded
`location` field; the normal scoped lifecycle persists only the coarse marker.

The coarse location statement is a separate existing `answer` content reference;
the closed TranscriptFinish object gains no optional fields. A successful scoped
finish records one `location_used` ledger event from that exact server-owned
statement, with no coordinates or resource references. The transcript ledger
and outbox RLS policies include this event kind while retaining all existing
organization, membership, turn-owner, conversation and action predicates.
Operators apply the existing project schema upgrade before deploying the updated
scoped ledger behavior; this task applies it only to disposable test databases.

## Presentation amendment, 2026-10-25
REQ-NEARBY-01 presentation acceptance: render local times in the request language,
with tomorrow or a localized date for later transitions; never expose ISO timestamps.
Round approximate distances to readable metres/kilometres with localized decimal
separators, explaining straight-line distance once. Show 3–5 available choices,
grouped by open now, opening later, and unknown schedules. If the closest known
open result exceeds 1.5 km straight-line, say the open results are far, walking
may be longer, and offer a wider or narrower area; never invent a walking route.
Use short source labels with Atlas numbered citations, no visible raw URLs.
Separate country-formatted phone numbers from links. Localize the request-only
location/OSM attribution footer; provider codes remain internal diagnostics.
The internal coarse ledger label and location privacy contract remain unchanged.
Own recorded/mobile expectations are updated for this owner-requested behavior.
This session is local-only, EUR 0.20 maximum, at most five Nominatim requests.

Presentation dependencies: pin Babel 2.18.0 (BSD-3-Clause, already installed as a
transitive dependency) for locale date names, avoiding hand-maintained calendars.
Add phonenumberslite 9.0.41 (Apache-2.0, 473,205-byte universal wheel) for national
numbering plans, international display and extensions; omit geocoder/carrier data.
Handwritten country regexes cannot reliably handle variable-length numbering plans.
Both are CPU-only and make no network calls while formatting. Upstream sources:
https://babel.pocoo.org/en/latest/api/dates.html and
https://pypi.org/project/phonenumberslite/9.0.41/.
