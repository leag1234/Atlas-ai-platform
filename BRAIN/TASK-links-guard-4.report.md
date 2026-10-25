# Links guard 4 — bounded task report, 2026-10-25

Implementation locally verified; the live coverage expectation remains unmet.
This is not a milestone run. No GitHub CI result is claimed. The single authorized live check
fell short of the requested two-to-three-shop coverage; the subsequent rendering
fix is verified offline against its authentic search responses, not by another
live conversation.

## Changes

- Restored standalone price questions to their previous routing. Explicit links,
  product pages, shopping and related follow-ups receive mandatory discovery/page
  reading. Discarded the uncommitted m22 gate acquisition changes. J34's existing
  cassette and the original m22 assertions remain unchanged.
- Retained empty-bullet/dangling-label cleanup, removal of sections emptied by
  citation filtering, and the setext-heading fix. Link replies are buffered until
  provenance validation, and failed retrieval gives an honest limitation with
  context-bearing search terms. Price filtering no longer corrupts fallback terms.
- Target shopping searches using model identifiers from the conversation. One
  query per model combines shop site filters, respecting the unchanged three-search
  request quota. No product names are embedded in production routing. The bounded
  automatic renderer recognizes Thomann, Woodbrass, Boulanger and Fnac; ordinary
  model answers remain available for other returned domains.
- Product URLs returned by search count as retrieved even if shop fetching fails.
  Available matching shop results are rendered directly, deduplicated by shop,
  with up to three shops per model. Missing models receive an explicit limitation
  and search terms. Model/title matching and shop recognition are conservative;
  they do not guarantee results for every product or retailer.
- Prices require the same linked URL's fetched text or search snippet. Retained
  amounts are labelled with their evidence source/domain and “at the time of
  reading” in the answer language. Prices are not transferred between shops.
  Automatic snippet rendering omits ambiguous multiple amounts. This verifies
  textual evidence, not whether an amount is the current checkout price.
- Added regressions for standalone prices, contextual requests, targeted searches,
  blocked fetches, snippet attribution, cleanup, bounded recovery and public HTTP
  delivery. Added authentic SearXNG tool-response replay from the one live check.
  Synthetic SearXNG-shaped data is used only in separate unit tests. The recorded
  test uses scripted model output to test missing-link recovery; it is not a
  provider-completion replay or a new J34 acquisition.

## Live evidence and limits

Source: `BRAIN/links-live-4/hetzner.capture.json.gz`, `hetzner.report.json` and
`budget.json`. Exact questions:

1. “Quels pianos numériques me conseilles-tu pour débuter, budget 500 euros ?”
2. “Donne des liens chez thomann ou autre”

Used the owned bundled SearXNG and nonpersistent quick-start stack, Hetzner preset
only, offset 26502. Turns took 12.569 s and 17.138 s. Three searches were made;
no paid search provider was used. Live output linked one Yamaha shop and one
Roland shop, with no Casio shop. Therefore the requested 2–3 links per recommended
model was NOT met. The harness's broad “links or limitation” flag is not evidence
of that shopping expectation.

The searches actually returned two usable Yamaha shops and two Roland shops.
The Casio search returned `upstream_blocked` after its upstream engine attempts.
The model omitted some retrieved links and left dangling shop labels. Offline
rendering fixes now reproduce four useful links from those exact responses:
Yamaha: Woodbrass and Thomann; Roland: Thomann and Fnac. Casio gets an honest
retrieval limitation and search terms. No further live check was run or recorded.
The recorded regression fails when the new rendering is disabled, and succeeds
with it enabled.

NOTICED BUT NOT TOUCHING: first-turn piano specifications contain unverified
model-authored claims. This bounded link task does not certify those claims or
recommendation quality. No upstream SearXNG policy, timing threshold, provider
assertion or protected file was changed.

## Local verification

Final summaries and commands: `BRAIN/TASK-links-guard-4.checks.txt`.
The first complete keyless suite ran 1,752 tests in 907.577 s, exit 0, OK.
Journey replay consumed unchanged J34 under its existing accepted exception,
then failed the original J36 assertion:
cleanup incorrectly removed numbered instructions containing inline bold text.
Two added regression tests, including the exact J36 recorded answer, failed
before the fix. Numbered-heading detection now preserves inline emphasis.
The unchanged J36 assertion and cassette were not edited.

After this fix: 93 focused tests in 4.242 s, OK; lint/format and strict typing
cover 503 files, exit 0. Final-code full keyless suite: **1,754 tests in
815.881 s, exit 0, OK**. No SearXNG flake exception was needed.
**Complete `make test-journeys`: exit 0**; the unchanged validator accepts the
J1–J53 replay report. J36 and J53 are true. J34 replays the original cassette
exactly and retains the gate’s pre-existing accepted distinct-price-measures
regression (`J34_spacex_search_first: false`); no assertion was weakened or
exception added. The earlier complete keyless result is retained separately.
An initial full-suite run was interrupted for final typing fixes; its log is
retained separately and is not claimed complete. One broad focused invocation
named a nonexistent test module; the corrected invocation uses existing modules.
The genuine-recording regression deliberately fails with rendering disabled.

## Cost and resource ownership

This session: EUR 0 model cost under the configured free Hetzner preset, EUR 0
paid search cost, three SearXNG searches against a ceiling of six. The wrapper
bounds live cost at EUR 0.10 and rejects paid search providers. No new cloud/GPU
resources; incremental infrastructure rental EUR 0/hour. Earlier reports retain
the prior sessions' accounting and incidents; their spend is not relabelled.

Preflight checked listener ports and absence of matching containers/volumes for
26500 (initial journey candidate), 26501 (keyless tests), 26502 (live).
While waiting for serialization, 26500 became occupied at ports 34500/34510.
A fresh preflight confirmed 27500 free of listeners and matching Docker resources;
journeys use 27500. No occupied service was touched. The live stack stopped and its
owned, unattached SearXNG cache volume was removed. After all checks ended, no owned containers remained at offsets 26501, 26502 or
27500. Removed the three unattached journey volumes created by this session.
No unrelated resource was stopped. Cleanup evidence: `BRAIN/links-4-cleanup.json`. The full suite and journeys use sanitized child environments, disable
secret auto-loading and use only startup placeholders when required. A running
full-suite environment audit found no paid credential variables.

## Scope and handoff

CONTRADICTION: current bounded-task instructions supersede milestone/PR rules and
the earlier standalone-price acquisition plan. This session only commits locally
on links-guard after local checks; no push, PR, merge or new branch.
ASSUMPTION: combining shop site filters per model is necessary to cover up to three
models within the existing three-search request quota; quotas were not changed.
Named prior task markdown files were absent inside this worktree; the three prior
reports supplied their state. Work remained in this worktree.

Final review: the eleven changed source/test files match their checked SHA-256
snapshot; protected paths are unchanged; J34 cassette and m22 gate are byte-identical
to HEAD. Scoped credential-value scanning and `git diff --check` are clean. The
single local commit includes code, tests, authentic tool fixtures, this report and
the check summary. No live acceptance or GitHub CI completion is asserted.
