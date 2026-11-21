# App render check: Atlas opens the app before saying it is done

Status: implemented behind `ATLAS_APP_RENDER` (off by default; `docker` on staging).
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

## Not covered

- Whether the app does what was asked (a judge or scripted interactions per request
  type; the benchmark of docs/26 measures it offline).
- Drawings (SVG without script): the static checks remain.
