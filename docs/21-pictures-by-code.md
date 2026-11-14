# 21. Pictures by code

Status: integrated behind feature flags, off by default. Staging first; the
acceptance corpus of the design note runs there before anything is called ready.

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

## 5. Limits

- Not a photograph, not a realistic portrait, not a likeness of a real person.
- Hats over the eyes remain the most frequent face defect; people on a boat may
  stand in front of the hull; horses and other large animals depend on the
  provisional animal kit.
- A chosen picture above 96 kB of SVG is too large to be given back to the model
  for a change.
- Strict privacy mode is not wired: the painter role runs on a hidden profile.
- The contact sheet is one image: on a phone the four pictures are small; a tap
  on a number shows one full size.
