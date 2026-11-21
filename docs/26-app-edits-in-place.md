# App follow-ups: edits in place instead of whole rewrites

Status: design note, not implemented. Scope: the side-panel app and drawing turns
(`services/orchestrator/app_turn.py`).

## Problem, measured

Every follow-up on an app ("fix the tempo", "add two acoustic kits") makes the model
write the whole file again (`CURRENT_SOURCE`: "write the COMPLETE new file"). On a
staging session of 2026-11-20 (a 16-step drum machine, about 320 to 370 lines,
10.7 to 13 KB), measured from the staging journal and the artifact store:

| Turn | Lines changed vs previous version | Output tokens | Turn time | Cost (EUR) |
|---|---|---|---|---|
| v1, create | (new, 318 lines) | 3 958 | 130 s | 0.0085 |
| v2, two fixes | 59 | 5 856 | 116 s | 0.0144 |
| v3, sound bank menu | 121 | 4 550 | 93 s | 0.0124 |
| v4, two acoustic kits | 27 | 5 015 | 171 s | 0.0137 |
| rewrite after a lost version | (new file) | 8 409 (2 calls) | 245 s | 0.0332 |
| next fix | (whole file) | 8 171 (2 calls) | 129 s | 0.0377 |

Output runs at about 30 to 50 tokens per second on the app profile, so the turn time
is the length of the file, not the size of the change: v4 changed 27 diff lines
(about 7 % of the file) and took almost three minutes. A whole rewrite also risks
dropping features the request did not mention (the owner: "you removed features").

## What others do (survey)

- Aider (open source, Apache-2.0) documents several edit formats: "whole" (the full
  file), "diff" (SEARCH/REPLACE blocks: an exact excerpt of the current file and its
  replacement) and "udiff" (unified diff). Its documentation recommends the diff
  formats for capable models (less output, so faster and cheaper), and its unified
  diff benchmark reported fewer answers that omit code ("lazy" answers) than other
  formats.
- Anthropic's text editor tool (`str_replace` with `old_str` / `new_str`) and many
  coding agents (OpenCode, which Atlas Work already runs) use the same idea: an
  exact, unique excerpt replaced by new text, refused when the excerpt is missing or
  ambiguous, so the edit is checked by code before it is applied.
- Fast "apply" models (published by Cursor and others) rewrite a file from a sketch
  with speculative decoding. They need a dedicated model and serving; not considered
  for Atlas now.

Licence and sovereignty: the format is a convention, no dependency is needed;
everything runs in the orchestrator with the current models.

## Proposal

1. Follow-up on an app whose current source exceeds a threshold (proposed 120 lines):
   the system message gives the current file with line numbers stripped, and asks for
   one fenced `edits` block of SEARCH/REPLACE pairs, or the whole file when the
   change rewrites most of it. Small files keep the whole-file path.
2. Code applies the pairs in order: each SEARCH must match exactly once (after
   normalising line endings and trailing spaces); a pair that matches zero or several
   times fails the edit. Nothing is applied partially.
3. The result goes through the same `prepare` validation as today (HTML parse,
   script syntax, no network, no dialogs), then is stored as the next version with
   `base_version` set, as today.
4. One repair: when an edit fails to apply or validation rejects the result, the
   same call is retried once with the reason (which SEARCH did not match, or the
   validation problems), and the second failure falls back to one whole-file call
   within the turn deadline. The person sees the same plan (write, check, deliver);
   progress counts edited lines.
5. The answer describes the change from the model's sentences after the block, as
   now, and the footer links the stored version only (artifact_links).

Expected effect, from the table: a 27-line change is a few hundred output tokens
instead of 5 000, so tens of seconds instead of two to three minutes, at a fraction
of the cost; features outside the edited excerpts cannot be dropped.

## Measurement before the switch

- A continuation benchmark (the app corpus of `tests/test_artifact_benchmark.py` plus
  five multi-turn sessions in two languages, realistic phrasings): edit success rate
  (applied and valid on the first call), fallback rate, turn time p50/p95, output
  tokens, and a browser check of every version (renders, no console error, the asked
  change is present, nothing else removed).
- Threshold and switch behind `ATLAS_APP_EDITS` (off by default) until the benchmark
  shows the edit path at least as reliable as whole rewrites.

## Open points found on the same session (not addressed here)

- "Check the app" is static: block received, HTML and script parse, no network or
  dialog API. It does not open the app in a browser, so a page that throws at load or
  renders nothing passes. A headless render (the preview CSP, a console error or an
  empty body fails the check) needs Chromium in the orchestrator image; it belongs to
  the same benchmark work.
- The preview blocks all network access (`connect-src 'none'`, `media-src data:
  blob:`), so an app cannot load samples or images from other sites. Two ways to give
  apps real media without opening the sandbox: the person attaches the files, or
  Atlas fetches files the person named (size and type bounded, licence stated) and
  embeds them as `data:` URIs in the stored file. Both need an owner decision.
