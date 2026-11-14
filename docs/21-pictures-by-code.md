# 21. Pictures by code

Status: integrated behind feature flags, off by default. Staging first; the
acceptance corpus of the design note runs there before anything is called ready.
Pictures drawn on a CPU server by an open image model (section 5) answer first
when `ATLAS_PICTURES_CPU=1`; painting by code stays their fallback.

## 1. What the user gets

A request for a picture, in any wording, gets **four candidate pictures** in one
numbered contact sheet, the one the code ranks best first and framed. The user
clicks a number (the picture opens full size and becomes the chosen one) or
answers in the chat ("the second", "2", or the same in any of the five
interface languages). Without a choice, the
first one counts. The chosen picture is the conversation's drawing artifact:
"make the fox bigger" changes it through the ordinary modify path (one draft,
no new choice). API callers get the same answer: the sheet and the four
numbered links (each returns its picture).

No image model and no GPU: a text model writes SVG drafts, code finishes them.
The drawing is that of a careful amateur with cartoon faces. A request for a
photograph or a realistic picture gets the illustrations and one sentence
saying they are not photographs, never a refusal.

## 2. The chain

| Step | What | Where |
|---|---|---|
| 1. Checklist | One JSON call: things, counts, relations, actions, exact text; code removes whatever the request does not contain | `pictures/checklist.py`, role `picture_checklist` |
| 2. Four drafts | Four parallel calls, sampling temperature 0.7, the measured painter prompt (round 10 baseline, face block, body block BODY4, animal block ANIMAL2 when the animal kit is on, labels block for diagrams) | `pictures/painter.py`, role `picture_painter` |
| 2b. Living map | Which characters are living beings (one short call, during the drafts) | `pictures/checklist.py` |
| 3. Recover and sanitise | Zero-token XML salvage; no script, external resource, event attribute or entity; size bounded | `pictures/svg_text.py`, `pictures/sanitise.py` |
| 4a0. Animal kit (provisional) | The painter draws the animal's head and declares a pose; code draws the body for dogs, horses, cattle, sheep and goats, pigs, elephants, giraffes | `pictures/animal_kit.py`, own flag |
| 4a. Body kit | The painter draws each person's head and declares a pose; code draws body and clothes, solves contacts, applies relations (in, on, behind, lying on or in) | `pictures/body_kit.py` |
| 4b. Labels by code | Diagrams and requests for labels: the painter names the parts (`data-label`) and writes no label; code writes the labels in the margins with non-crossing leader lines | `pictures/labels.py` |
| 5. Measure | Judge (version 2) and the four code checks (detached part, polygon in a face, depth order, readability) | `pictures/judge.py`, `pictures/checks.py`, `pictures/ordering.py` |
| 6. Face kit | Eyes, brows, nose or muzzle, mouth and cheeks redrawn from a small kit at the painter's positions; a hat over the eyes is raised unless the request covers them | `pictures/face_kit.py` |
| 7. Finish | A contour around opaque figures (not smoke, light, water or line drawings) and a contact shadow | `pictures/finish.py` |
| 8. Order and show | Fewest failed checks first, then the judge's quality; every finished draft is shown | `pictures/turn.py`, `pictures/chain.py` |

Steps 3 to 7 run in the **picture worker**: one child process per draft, at most
two at once, with an address-space and CPU limit, a wall-clock limit enforced by
the parent, and an environment without secrets (`pictures/worker.py`,
`pictures/job.py`). The event loop never waits on that work.

The prompts and every rule above were measured in spike rounds 10 to 23; the
ports reproduce the spike outputs byte for byte on the round 23 draws (body,
animal and face kits, contour finish) and the judge and check numbers exactly.
Labels by code on SVG drafts are not measured yet.

## 3. Configuration

| Variable | Default | Effect |
|---|---|---|
| `ATLAS_PICTURES_BY_CODE` | off | `1` turns the picture turn on |
| `ATLAS_PICTURE_ANIMAL_KIT` | off | `1` adds the provisional animal kit (its own switch) |
| `ATLAS_HIDDEN_PROFILES` | empty | must include the painter's hidden profile (see `picture_roles`) |
| `ATLAS_PICTURE_SOFT_SECONDS` | 120 | painting time after which the drafts already painted are kept (30 to 240) |
| `ATLAS_PICTURE_BRIEF_PLAN` | off | `1` asks the painter for a short plan before its SVG (shorter reasoning, see section 4) |
| `ATLAS_PICTURE_SPARE_DRAFTS` | 0 | drafts started beyond the four (0 to 2); the first four painted are kept, the others stopped; started only while each draft keeps a whole share of the turn's ceiling (0.028 EUR, its full output room): one spare needs `ATLAS_PICTURE_TURN_EUR` of 0.15 |
| `ATLAS_PICTURE_BOUNDED_CONTOURS` | on | `0` keeps the contour filters canvas-wide, as in the spike (see section 4) |
| `ATLAS_PICTURE_JOB_SECONDS` | 90 | wall-clock limit of one draft job |
| `ATLAS_PICTURE_JOB_MEMORY_MB` | 2048 | address-space limit of one job |
| `ATLAS_PICTURE_FONT_DIR` | Liberation paths | directory of the Liberation fonts |
| `ATLAS_PICTURE_DIR` | `<ATLAS_IMAGE_DIR>/pictures` | picture sets (candidates and choice) |

`picture_roles` in `services/model-gateway/routing.yaml` binds each step to a
declared profile, so no model name appears outside the gateway. Profile calls
accept an optional `sampling_temperature` (0 to 1); unset, the provider body is
unchanged (temperature 0).

The runtime needs `resvg-py`, `numpy` and `scipy` (pinned in
`requirements-dev.txt`) and the Liberation fonts (`fonts-liberation` in the
image; the host needs `/usr/share/fonts/truetype/liberation`).

## 4. Cost, time, failures

- One ceiling of 0.10 EUR per request bounds every call (checklist, living map,
  four drafts). Measured in the spike: about 0.04 to 0.05 EUR per request.
- About 100 to 130 s of wait (measured median 128 s with the body kit). After
  120 s of painting the turn keeps the drafts already painted (at least one).
  A draft stopped at that point may still be billed by the provider: its whole
  reservation is counted in the turn's cost.
- First integration run (2026-11-13, six requests, painter account shared with
  a spike run): 108 to 126 s per request; four candidates on two requests, three
  on one, two on two, one on one (the others were still painting at 120 s);
  0.039 to 0.041 EUR for the requests that kept four drafts, up to 0.084 EUR
  counted when drafts were stopped (reservations included).
- Where the time goes (2026-11-14, 10 corpus requests, 40 drafts streamed from
  the painter with the turn's own request body): a draft writes 85 to 100
  tokens a second; the SVG itself is a median 5300 tokens (about 60 s), the
  reasoning a median 1300 tokens but 4200 at the 90th percentile and up to
  11300, which is what cuts a draft at its output limit. A draft cut there
  lacks the things the request names (they are drawn last), so it is lost,
  not mended. The slowest of the four drafts sets the wait.
- The short plan (`ATLAS_PICTURE_BRIEF_PLAN=1`): reasoning median 960 and
  90th percentile 1900 tokens, draft median 71 s instead of 88 s, cost per
  draft -10 %, the same share of requests with a good picture (10 of 10, blind
  contact sheets). A size target for the SVG cut it by a third but lost the
  people's poses on two requests of ten: not kept. Starting the drafts 8 s
  apart changes nothing (the provider's rate per draft does not depend on how
  many run).
- One spare draft (`ATLAS_PICTURE_SPARE_DRAFTS=1` with a ceiling of 0.15 EUR)
  drops the slowest draft from the wait: with the short plan, replayed on the
  measured drafts, four candidates on 10 requests of 10, wait median 107 s and
  at most 122 s, about +20 % cost (the stopped draft is billed to the end: the
  gateway does not stop a call its caller left). With a 0.12 EUR ceiling the
  fifth share would lower every draft's output limit to about 9000 tokens and
  cut long drafts, so no spare is started.
- The code steps of a draft ran 5 to 25 s, most of it in the final render: the
  contour finish filtered a canvas-wide region for each outlined group.
  Bounded to the group (`finish.bound_contours`), the render takes 0.3 to 1.6 s
  instead of 0.7 to 13 s, with the same picture to a few antialiased edge
  pixels; a whole draft job, median 6.3 s instead of 9.5 s, 90th percentile
  9 s instead of 16 s (50 drafts). Two jobs run at once per process, so with
  several requests at a time the drafts queue behind each other's jobs: in a
  run of three requests at a time, painting was over by 56 to 107 s but two
  requests waited 137 and 155 s.
- D5: a budget or quota refusal from any call ends the turn with the measured
  refusal. A provider failure drops only its draft: fewer than four pictures are
  shown, with one sentence saying so. With no draft left, the drawing failure
  sentence.
- Only the chosen picture is an artifact; the candidates stay as generated
  images and in the picture set for 30 days.

## 5. Pictures on CPU (ATLAS_PICTURES_CPU=1)

With the flag on (and `ATLAS_PICTURES_BY_CODE=1`, which stays the fallback),
a picture request is first drawn by an open image model on a CPU server.
Choices come from spike rounds 24 and 25 (2026-11-13): blind grades 4.84 of 5
against 2.56 for painting by code on scenes, 0 of 116 pictures photographic
with the rewrite, about 25 s per picture.

| Step | What | Where |
|---|---|---|
| 1. Server check | Health and queue: if the two pictures cannot be done within `ATLAS_PICTURE_CPU_SECONDS`, painting by code answers | `pictures/cpu_turn.py` |
| 2. Rewrite | One JSON call: an English prompt in one fixed flat illustration style (code appends the style line and strips photographic words), whether it is a diagram, the words to place in the user's language, a title, the counts, colours and unusual features to check (`checks`) and the words to show (`text`); violent, cruel, hateful or sexual requests are refused | `pictures/rewrite.py`, role `picture_rewrite` |
| 3. Two pictures | Two seeds, drawn one after the other (one queue per server), 512 px, 4 steps, guidance 1, Lanczos to 768 px | server (`infra/pictures-cpu/gate.py`) |
| 4. Filter | Every picture is scored by a CPU image classifier on the server; a score of 0.5 or more, or no score, and it is never shown | server, `cpu_turn.shown` |
| 5. Diagram labels | The picture is wordless; one call on the gateway's `locate` vision role gives where each part is drawn; code writes the words in side margins with uncrossed leader lines (label16 rule) | `pictures/locate.py`, `pictures/cpu_finish.py` |
| 5b. Words to show | A name on a logo, a greeting on a card, a title on a cover: the rewrite returns the words as the user wrote them (code takes accents, quotes and apostrophes from the request itself), the prompt asks for a plain empty band and never names the words; code sets them in the emptiest band (largest size that fits, even line breaks, dark or white ink by WCAG contrast, a plate below 4.5:1) in the bold Liberation font (`LiberationSans-Bold.ttf`, SIL OFL), and the SVG artifact keeps them as real `<text>` | `pictures/text_setting.py`, `pictures/cpu_finish.py` |
| 5c. Detail check | When the request gives counts, colours or unusual features, one call per shown picture on the `locate` vision role says which `checks` the picture does not show; the answer names them ("may not show ..."), or says they could not be checked when the call fails; nothing is refused or promised | `pictures/cpu_checks.py` |
| 6. Show | Two candidates in the contact sheet; choice and changes as in section 1 | `pictures/turn.py`, `pictures/choice.py` |

A change ("make the cat bigger") rewrites the prompt with the change and draws
it again with the chosen picture's seed, as the next artifact version. The
artifact is an SVG document holding the picture (JPEG). When the server cannot
answer a change, the picture stays as it is (unchanged-version sentence).
"Change the name to ..." gives new words to the same rewrite, so the next
version carries them; the size always fits the band (a request for a bigger
name does not make it bigger than the band allows).

Fallback: server not configured, unreachable, not ready, busy, a rewrite that
fails or cannot be read, no picture drawn in time, or a diagram with no part
located: the turn is painted by code, with no user-visible error; the log line
is `picture_cpu_fallback code=...` and `picture_cpu_fallback` is in the
interaction's errors. Whatever the CPU attempt spent is kept in the turn's cost.

D5 and cost: a budget or quota refusal of the rewrite or locate call ends the
turn with the measured refusal. The rewrite (about 0.0005 EUR) and the locate
calls (about 0.0004 EUR each) and the detail checks (about 0.0002 EUR each,
under 2 s) count in the turn's cost; the server is a fixed
cost, 0 per picture, and its render seconds are logged
(`picture_cpu_render_seconds` in the interaction's latencies).

| Variable | Default | Effect |
|---|---|---|
| `ATLAS_PICTURES_CPU` | off | `1` draws on the CPU server first |
| `ATLAS_PICTURE_CPU_URL` | unset | `https://<server address>:8790` |
| `ATLAS_PICTURE_CPU_TOKEN_FILE` | unset | file holding the server's shared token |
| `ATLAS_PICTURE_CPU_CA` | unset | the server's self-signed certificate (PEM) |
| `ATLAS_PICTURE_CPU_SECONDS` | 100 | longest wait for the pictures (30 to 200) before painting by code |
| `LOCATE_MODEL` | routing.yaml | model of the gateway's `locate` role |

### CPU picture server

`infra/pictures-cpu/`: the engine (stable-diffusion.cpp, MIT, pinned commit,
built for the host CPU), the gate (`gate.py`: TLS, shared token, one queue,
fixed sizes and steps, the content classifier), the pinned weights with their
sha256 (`weights.json`; FLUX.2 klein 4B converted to 4 bits by the engine's
converter, Qwen3-4B Q8_0 text encoder, klein VAE; Z-Image-Turbo 4 bits as the
fallback model, `install.sh ADDRESS zimage`; the classifier
nsfw_image_detection, all Apache-2.0), pinned wheels, and `scw_server.py`
(the Scaleway Instance: create, list, stop, start, delete).

Deploy (from the Atlas host, Scaleway keys in the environment):

1. `python3 infra/pictures-cpu/scw_server.py create fr-par-2 COMPUTE3-X16C-32G 50 <atlas host IP> <ssh public key>`
   (security group: SSH and 8790 from the Atlas host only).
2. Copy `infra/pictures-cpu/` to the server, then as root:
   `bash install.sh <server IP>` (builds the image, fetches and checks about
   15 GB of weights, makes the token and certificate once, blocks new outbound
   connections from containers, starts the container with restart on boot).
3. Copy `/opt/pictures-cpu/secret/cert.pem` and `token` to the Atlas host
   (mode 0600) and set the variables above.

Cost: COMPUTE3-X16C-32G with a 50 GB volume and its IP, about 0.48 EUR per hour
(11.5 EUR per day) kept on. `scw_server.py stop` powers it off (the volume and
the IP are still billed); `start` powers it on and the container restarts by
itself; meanwhile the turn paints by code.

## 6. Limits

- Not a photograph, not a realistic portrait, not a likeness of a real person.
- Hats over the eyes remain the most frequent face defect; people on a boat may
  stand in front of the hull; horses and other large animals depend on the
  provisional animal kit.
- A chosen picture above 96 kB of SVG is too large to be given back to the model
  for a change.
- Strict privacy mode is not wired: the painter role runs on a hidden profile;
  the CPU picture server is a rented server outside the owned inference path.
- The contact sheet is one image: on a phone the four pictures are small; a tap
  on a number shows one full size.
