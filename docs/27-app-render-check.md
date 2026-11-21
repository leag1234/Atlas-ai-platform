# App render check: Atlas opens the app before saying it is done

Status: implemented behind `ATLAS_APP_RENDER` (off by default; `docker` on staging).
Timers, pictures, sound and the follow-up comparison added after staging -142.
Scope: the "Check the app" step of side-panel app turns
(`services/orchestrator/app_turn.py`, `services/orchestrator/app_render.py`).

## Problem

"Check the app" was static: a block was received, the HTML and the scripts parse,
no network or dialog API is written (`packages/artifact_html.py`). A page that
throws when it loads, renders nothing, or needs what the preview blocks passed the
check and was handed over (docs/26-app-edits-in-place.md, open points). The owner's
rule of 2026-11-16 is that Atlas checks its own result against the request before it
says done.

## What others do (survey)

- ArtifactsBench (Tencent Hunyuan, 2025, open source: github.com/Tencent-Hunyuan/
  ArtifactsBenchmark, arXiv 2507.04952) renders each generated artifact in a headless
  browser and captures screenshots before, during and after interaction, then has a
  multimodal model score them. Atlas keeps the browser part and replaces the judge by
  deterministic checks: a judge model would add cost and latency to every turn.
- Playwright (Apache-2.0, maintained by Microsoft, already pinned in
  `requirements-dev.txt` and used by the Atlas UI tests and the artifact benchmark in
  `evals/artifacts/browser.py`) drives Chromium and reports uncaught exceptions
  (`pageerror`), console messages and requests, and can serve a page by request
  interception with chosen headers. No new dependency is needed.

## What the check does

The stored file, with the storage shim the side view inserts, is served by request
interception at a fixed https address with the side view's own headers
(`artifact_routes.PREVIEW_CSP`, `sandbox allow-scripts`, `connect-src 'none'`, the
same Permissions-Policy). Every other request is aborted and recorded. The page gets
6 s to load and 0.8 s to settle, then up to three visible buttons are pressed. The
check fails on:

| Code | Seen |
|---|---|
| `page_error` | uncaught exception or rejected promise while loading |
| `control_error` | uncaught exception after pressing a button (the label is kept) |
| `csp_violation` | `securitypolicyviolation` event (directive and blocked address) |
| `blocked_request` | a request that is not the app itself (navigation) |
| `blank_page` | no visible text, control, drawing, image or painted box |
| `dead_controls` | visible buttons and no event handler anywhere in the page |
| `not_loaded`, `not_responding` | load not finished in 6 s, or the page hangs after |
| `later_error` | uncaught exception once the page's timers have run 30 s with nobody touching it |
| `broken_image` | an `<img>` without a source, or whose source does not decode, shown or hidden |
| `audio_without_gesture` | an `AudioContext` created, or a media element played, before the first press or key |

Timers run on Playwright's fake clock (`page.clock`, installed before the page
loads; time still flows): after the first inspection the clock is run forward
30 s in 1 s steps within 2.5 s of real time, and 2 s more after the presses, so a
countdown that ends, the next question or a sound at the end of a turn are seen.
A callback that throws during that run surfaces from `clock.run_for`, not as a
`pageerror`, and is recorded as `later_error`. `Math.random` follows a fixed
sequence in the check, so the same file shows the same states at every check.

Sound: Chromium keeps an `AudioContext` created before a user gesture
suspended ("The AudioContext was not allowed to start"), and headless Chromium
does not log that warning. The page's own wrapper of `AudioContext`,
`webkitAudioContext` and `HTMLMediaElement.play` records a start before the
first trusted `pointerdown`, `mousedown`, `keydown`, `touchstart` or `click`.
`navigator.userActivation` cannot be used: Playwright's `evaluate()` calls count
as a gesture for the browser.

`broken_image` and `audio_without_gesture` are soft: the page works otherwise.
They get the one repair like the others; if the repaired file still has one (or
the repair breaks the page), the working file is delivered and the answer says
what does not work (`user_errors.app_soft_defects`).

## Follow-ups: the current version first, and nothing lost

Staging -142 (2026-11-21): after "it does not work, nothing shows", the model
guessed a cause, rewrote code that worked (how the embedded flags were written)
and said that had been the problem. Two changes close that class for any app:

- Before the model writes a follow-up, Atlas opens the current version with the
  same check (in parallel with the media downloads) and gives the model what it
  saw, or that it saw nothing wrong, with the instruction to fix measured causes
  and never claim a cause that was not found.
- After the new file passes the check, `services/orchestrator/app_features.py`
  compares it with the current version, as PR 591 does for documents
  (`readback.changes`): element ids, the kinds of parts (pictures, canvas, SVG,
  sound and video elements, sliders, lists, tables), the number of embedded
  `data:` pictures and sounds, the `atlas-media` files, the abilities of the script
  (timer, animation, sound, keyboard and touch controls, drawing, saving, speech,
  vibration, full screen, drag and drop) and the visible texts. A text alone is
  most often the requested change, so texts are listed only with a structural
  loss. Losses get the one repair, which restores them unless the request asked
  for the removal: the model then answers `KEEP` and the file it wrote is stored
  without a second write. A version stored with losses still open says so in the
  answer (`user_errors.app_lost_features`, element ids aside, which the person
  does not see); the description never claims them.
- Every repair of a follow-up (a file rejected by the static checks or by the
  render check) says to start again from the stored version and never write the
  app from scratch, and lists what the rejected file had already lost, so one
  repair fixes both. Replays of the -142 turn on staging's gateway: the first
  file used the flags' web addresses, and its repair wrote the app again and
  dropped the sound.

On failure the turn makes its one repair call with the list of problems (the same
budget as a static rejection: at most two calls per turn). If the repaired file fails
too, nothing is stored and the person reads what the check saw in plain words
(`user_errors.app_render_failure`: an error, a blank page, files from the internet
that the preview blocks, buttons that do not react, a page that froze) with a retry;
for a follow-up the current version is unchanged. Console errors that the app logs
itself are not failures: apps log caught problems on purpose.

When the browser cannot run (no image, no Docker, the check would not fit before the
turn deadline), the report says `unavailable` or `skipped`, the turn keeps its static
checks and the log says so (`app_render_unavailable`). A missing browser never fails
an app. The time of each check is recorded in `latence_ms.render_check`.

## Runtime choice

Options considered:

1. Playwright inside the orchestrator process, on the host's Chromium. Fast (1.3 to
   1.8 s per check on the build VM, peak about 135 MB), but the serving processes run
   as root with Chromium's own sandbox off: model-written code, which the person can
   steer, would run unsandboxed in the server's process tree, and it inherits the
   serving processes' OOM protection (`OOMScoreAdjust=-600`). Rejected for hosted
   instances; kept as `ATLAS_APP_RENDER=local` for development and tests.
2. A long-running render service shared by instances. Saves the start of a browser
   (about 1 s) but keeps memory resident, needs a port or socket, a lifecycle and a
   health check, and a crashed page could affect the next check.
3. One throwaway container per check (chosen, `ATLAS_APP_RENDER=docker`): image
   `atlas-app-render:local` from `infra/docker/Dockerfile.app-render` (python slim,
   Playwright 1.62.0 and Chromium headless shell only; 1.25 GB on disk, shared by
   every instance of a host). `docker run --rm --pull never --network none
   --read-only --cap-drop ALL --security-opt no-new-privileges --memory 512m
   --pids-limit 256 --cpus 1`, non-root user, tmpfs for `/tmp`. The check script is
   not baked into the image: the orchestrator passes its own
   `app_render_worker.py` at run time, so a code change needs no image rebuild. The
   container is removed with `docker rm -f` when the check exceeds 15 s. Measured on
   the build VM: 2.2 to 2.8 s per check. With no network namespace other than
   loopback, the check is also immune to the `ERR_NETWORK_CHANGED` aborts that
   Docker's interface churn causes to host browsers on the build VM.

At most two checks run at once per process (`ATLAS_APP_RENDER_CONCURRENCY`).
The hosted instances already drive Docker (Open Terminal, project runners). The
compose trial's orchestrator container has no Docker client, so its check stays
static.

## Operations

- Build once per host, and after a Playwright upgrade:
  `make app-render-image` (no build context is sent).
- Enable: `ATLAS_APP_RENDER=docker` (staging first, `infra/instances/staging.env`).
- Leftovers carry the label `atlas.purpose=app-render`.

## SVG files named for an app

Problem (reported 2026-11-21): a person asked an app to show flags from Wikimedia
Commons, which are public-domain SVG files. The app media path
(`services/orchestrator/app_media.py`) refused SVG files because an SVG is a document
that can carry script. The model then wrote the Commons addresses into the app, the
side view's policy blocked them, and the person never got the pictures.

What exists (survey before building, owner rule of 2026-11-11):

| Project | Kind | Licence | Fit |
|---|---|---|---|
| DOMPurify (cure53) | sanitiser, SVG profile | MPL-2.0 or Apache-2.0 | needs a DOM (browser or jsdom); no Python path |
| svg-hush (Cloudflare, Rust) and py-svg-hush | SVG sanitiser | Apache-2.0 or MIT | built for this case; the Python binding is a small third-party package (0.3.0) |
| nh3 (ammonia) | HTML sanitiser | MIT | does not model SVG (namespaces, `use`, paint servers) |
| bleach | HTML sanitiser | Apache-2.0 | deprecated since 2023, no SVG model |
| CairoSVG | rasteriser | LGPL-3.0 | in-process parser with advisories CVE-2023-27586 (fetching external resources, fixed in 2.7.0) and CVE-2026-31899 and CVE-2026-107378 (fixed in 2.9.0 and 2.9.1); new native dependency |
| resvg / resvg-py | rasteriser | Apache-2.0 or MIT | no script, no network by design; new native dependency |
| librsvg (`rsvg-convert`) | rasteriser | LGPL-2.1 | used by Wikimedia for its thumbnails; new system package |
| Chromium, image mode | rasteriser | BSD | already in the render check image, isolated, no new dependency |

Choice: rasterise. A sanitiser must anticipate every way an SVG can run code or fetch
(`script`, event attributes, `foreignObject`, `href` and `xlink:href` on `use`,
`image`, `feImage`, `a`, animated `href` through `set` and `animate`, CSS `url()` and
`@import`, `xml-stylesheet`, entity expansion), and the cleaned SVG would still be
parsed by the person's browser inside the app. A raster leaves only pixels, whatever
the SVG holds. Atlas already runs Chromium in a throwaway container without network
for the render check, so rasterising adds no dependency and no new isolation to
review.

How (`services/orchestrator/svg_raster.py`, `svg_raster_worker.py`):

- All SVG files of a turn are converted in one container run (same image, flags and
  `ATLAS_APP_RENDER` switch as the render check; about 2 s on the build VM).
- Each file is parsed with `DOMParser` (an inert document): it must be an SVG root in
  the SVG namespace, well formed (the parser refuses entity expansion bombs) and have
  at most 40,000 elements. References to other documents are taken out and counted,
  since Chromium would draw a broken-image sign in their place.
- The file is then drawn through `<img src="data:image/svg+xml...">`, the mode where
  browsers run no script and load no resource, on a page whose policy allows data:
  images only; every request is aborted and logged. The drawn element is captured as a
  PNG (longest side between 256 and 800 pixels), and a capture with no painted pixel
  is refused as blank.
- Download rules are unchanged (https, public addresses, size and count limits,
  licence from the person, the site or the Commons API). The PNG counts towards the
  turn's size limits. A wiki file page (`/wiki/File:Name.svg` on a Wikimedia project)
  is read as its file (`Special:FilePath`).
- The answer lists the file as "SVG converted to a PNG picture" with its licence, and
  says when parts loaded from other addresses were left out. A file that cannot be
  drawn is named with its reason: not a valid SVG, too complex, blank without its
  external parts, conversion failed or too long, or the isolated browser is not
  available on this server (`ATLAS_APP_RENDER=off`).

## Not covered

- Whether the app does what was asked (a judge or scripted interactions per request
  type; the benchmark of docs/26 measures it offline).
- Drawings (SVG without script) that the model writes: the static checks remain.
  SVG files the person names are drawn as pictures (above).
