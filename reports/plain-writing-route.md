# Plain writing requests stay a text answer (2026-11-16)

## Defect

On staging (strict privacy, owned GPU), "write a short message (5 sentences)
to ..." was routed to the app panel (task_type artifact): 62 s, and an app
instead of five sentences in the chat.

Two causes, one class: a request for a text was read as a request for
something else.

- Lexical gate (`packages/artifact_intent.py`), used whenever the model
  router has no decision (router off, timeout, gateway error, invalid output):
  any writing verb ("écris", "write", "schreibe", "escribe", "génère") counted
  as an app request. A strict turn whose router call to the owned model fails
  or exceeds `ATLAS_MODEL_ROUTER_TIMEOUT` takes this path.
- Router text (`services/orchestrator/request_router.py`): "a document to keep,
  print or send is file" and "a letter as a document" sent a letter to a
  landlord to Work as a file.

## Fix

- `packages/writing_intent.py`: `plain_writing(text)` is true when the request
  names a kind of text (message, email, letter, note, poem, post, reply,
  review answer, speech, summary, list, ...) or a text operation (summarize,
  reply, rephrase, translate), and names nothing that only an app, a picture,
  code or a file can be (interactive words, game, page, HTML, code, drawing,
  picture, file, document, a format, print, table, CV). Quoted or pasted
  material after the request is not read.
- Lexical gate: a plain writing request is never an app (`app_intent` returns
  None; a change to the current app still returns "modify"), and is never a
  file delivery (`requests_file_delivery`).
- Router: `settle()` applies the code rules after the model call. An app,
  drawing or file decision for a plain writing request becomes "answer"
  (follow-ups to something delivered keep the model's kind). The machine rule
  that was inline in `route()` moved there unchanged.
- Router text: a text to write, read, copy or send yourself is "answer"
  whatever its length unless a file or a format is named; two examples.

No change under `prompts/`.

## Evaluation

Cases: `tests/fixtures/plain_writing_routes.json`, 35 writing requests
(FR, EN, DE, ES, IT, typos, pasted paragraphs and reviews) and 15
counter-examples (7 apps, 8 files with a format). Deterministic part:
`tests/test_writing_intent.py`. Live part: `tools/writing_route_eval.py`
against the staging gateway, standard level, router on the fast profile.

Rows: expected; columns: where the turn goes (text in the chat, app panel,
Work file).

Before (origin/main 20c593f):

| path | expected | text | app | file |
|---|---|---|---|---|
| router alone | answer (35) | 34 | 0 | 1 |
| | app (7) | 0 | 7 | 0 |
| | file (8) | 0 | 0 | 8 |
| lexical gates (no router decision) | answer (35) | 17 | 17 | 1 |
| | app (7) | 0 | 7 | 0 |
| | file (8) | 1 | 0 | 7 |

After:

| path | expected | text | app | file |
|---|---|---|---|---|
| router alone | answer (35) | 35 | 0 | 0 |
| | app (7) | 0 | 7 | 0 |
| | file (8) | 0 | 0 | 8 |
| lexical gates (no router decision) | answer (35) | 35 | 0 | 0 |
| | app (7) | 0 | 7 | 0 |
| | file (8) | 1 | 0 | 7 |
| pipeline (router, then `settle`) | answer (35) | 35 | 0 | 0 |
| | app (7) | 0 | 7 | 0 |
| | file (8) | 0 | 0 | 8 |

The remaining lexical miss ("hazme una hoja de cálculo ...", a spreadsheet
without a format word) predates this change and is outside its class; the
router gets it right. Router cost: about 0.0009 EUR per call, median 390 ms.

Regression check on the existing router set (`tools/router_eval.py`, 93
requests): router 92/93 before and 92/93 after (one different follow-up miss
each, both reproduced on main), lexical 54/93 before, 55/93 after.

## Live chain (chat pipeline of this branch, staging gateway, standard level)

Five writing requests (FR message, FR email with typos, EN LinkedIn post, DE
poem, ES reply to a quoted review) and three counter-examples (FR memory game,
EN pomodoro timer, FR letter in Word), on the default profile and on
atlas-qwen. Work escalation was recorded, not run.

| profile | writing: text in the chat | apps: app panel | Word letter: Work | cost |
|---|---|---|---|---|
| default | 5/5, 2 to 10 s | 2/2, 36 and 49 s | 1/1 | 0.026 EUR |
| atlas-qwen | 5/5, 2 to 3 s | 2/2, 43 and 32 s | 1/1 | 0.042 EUR |

Router failure (router profile made invalid, as when a router call fails or
times out), default profile: on origin/main the first two writing requests
built an app ("this small page shows the thank-you message"); on this branch
all five are answered as text in the chat (2 to 11 s).

Strict level: not measured live; the owned inference server was not reachable
during this work. The code rules and the lexical gate do not depend on the
model, so a strict turn whose router call fails now answers in the chat too.
`tools/writing_route_eval.py --privacy strict` runs the same set on the owned
server when it is up.
