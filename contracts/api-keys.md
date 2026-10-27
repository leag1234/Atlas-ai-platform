# K-A Atlas key boundary

Source: design 19 REQ-KEY-01..06; owner defaults 2026-10-27.

Public prefix: `/api/atlas/v1`. Authentication is one `Authorization: Bearer atl_...`
header, with no Cookie or Origin. Unknown, missing, expired and revoked keys return
401. Scopes are `assistant` and `model` only. A project selector returns 403 with
`missing_scope: project`; it is not silently interpreted as ordinary chat.

| Method and path | Contract |
| --- | --- |
| GET `/models` | OpenAI list of permitted aliases: `atlas`, configured profile IDs, `atlas-code` |
| POST `/chat/completions` | Existing ChatRequest for assistant; existing ChatInput for raw model; JSON or SSE |
| GET `/usage` | Current key's UTC day, request count, charged/unknown micro EUR and both daily limits |

The `atlas` alias maps to the default configured chat profile. Key scopes and project
membership are checked before dispatch. The edge discards caller identity headers.
The administrator-bound `budget_user` is the same forwarded WebUI user ID used by
DailyLedger; a separate key reservation is layered on that ledger. Shared cost and
search budgets cannot be changed by request body or headers. Requests to the browser
chat route remain subject to browser Origin policy, even if they carry a Bearer key.

Quota errors are OpenAI error envelopes, HTTP 429, containing `requests`,
`daily_requests`, `charged_micro_eur`, `reserved_micro_eur`, `daily_micro_eur`.
Scope errors are HTTP 403 with `missing_scope`. Quota reservation and settlement use
integer micro EUR and transactions; uncertain outcomes retain the reserved amount.
No project, Work or connector permission is implied by `assistant`.

The separate administrator HTTP service binds to loopback and also requires a
dedicated administrator Bearer secret. Forwarded, cookie and browser requests fail
closed. `POST /keys` accepts login, name, scopes (stable), or org/member IDs and
subject/budget_user (project/service migration), plus
lifetime_days (default 90, range 1..365), daily_requests and daily_micro_eur.
The response contains metadata and the secret once. GET `/keys` omits secrets/hashes;
DELETE `/keys/{id}` revokes; POST `/imports/{developer}` imports one legacy service
credential and its history. The CLI is a client of these endpoints and writes the
secret exclusively to a 0600 delivery file. Self-service is disabled.

Storage schema: `api-keys.sql`. New credentials use HMAC-SHA256 over the entire key
with an environment pepper; imported credentials retain SHA-256 and require rotation.
Store operations isolate the SQLite implementation from routing, allowing a later
PostgreSQL implementation. Daily audit summaries and failure events contain no
content. Artifact download authorization remains the browser policy in K-A; see the
explicit limitation in `runbooks/devapi.md`.

### Login resolution and clean API answers (K-A)

In stable mode use `--login tester` or `--login tester@example.invalid` with
`--name external --scope assistant --key-file /secure/delivery.key`; no internal
WebUI identifier is needed. The admin HTTP body likewise accepts `login`, `name`
and `scopes`. Configure `ATLAS_WEBUI_URL` (the private WebUI HTTP listener) and
`ATLAS_WEBUI_ADMIN_TOKEN` (an administrator session token, injected securely).
The administrator service reads `/api/v1/users/` with pagination and resolves the
email to the exact forwarded user ID used by the budget ledger. It does not create
users. A missing login returns `user_never_signed_in` (404): ask the user to sign
in once. An ambiguous local part returns 409: supply the full email. Inactive users
return 403. Directory failures return 503 and never select a guessed budget bucket.
Project mode retains org/member selection and can also resolve `login`.

Both `/api/atlas/v1/chat/completions` and plain `/v1/chat/completions` put only
the final cited answer in `choices[].message.content` / `delta.content`. API SSE
buffers answer text until the assistant finishes; progress remains available during
execution in the extra `atlas.steps` field, and the final frame includes the complete
`atlas.steps` and `atlas.sources` arrays. JSON responses include the same arrays.
Sources are retrieved URLs; inline citations stay in answer content.
Official OpenAI clients accept these extra fields. Browser code explicitly sets
`atlas_ui: true` to retain existing progress rendering; this is a presentation flag,
not authorization. The Atlas key edge always forces it off. No client header is
used to infer the presentation.

Legacy service-key imports retain their explicit service budget identity and do not
require an interactive login. This exception only imports an existing developer
credential as model scope; new stable keys still require a signed-in login.
