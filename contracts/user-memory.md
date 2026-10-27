# P1d personal memory — REQ-SHARE-04

The retrieval service owns personal memory. Its key is (server-selected organization,
authenticated budget user). Chat JSON never supplies either component. The internal
HTTP API requires a service credential and a forwarded authenticated subject.
Stable storage is SQLite; project-mode storage is PostgreSQL with forced RLS on
both organization and subject. The installation operator applies the migration.

Entries have id, kind (preference/context/recurring_topic/fact), text (1–500
characters), source=user, created_at and observed_at (UTC). Only explicit save
commands and deliberate Memory-view edits write entries. No model extraction.
Default caps: 100 entries, 5000 serialized tokens per user; context 300 tokens.
Preferences must fit the context cap together or the write is refused with the
measured size and limit. Other entries are ranked by lexical relevance and fill
remaining context space. The manifest records selected entry IDs and token count.
Project dispatch precedes selection: all private and shared project turns exclude
personal memory. Project writes cannot target it.

GET /api/atlas/user-memory lists entries and effective settings.
PATCH /api/atlas/user-memory/settings accepts {enabled: boolean} (default true).
PATCH /api/atlas/user-memory/{id} accepts {text, kind}.
DELETE /api/atlas/user-memory/{id} erases one; DELETE /api/atlas/user-memory erases all.
These controls authenticate the WebUI session and use its budget user ID. The
operator can disable all personal memory with ATLAS_USER_MEMORY_ENABLED=0 or an
organization using ATLAS_USER_MEMORY_DISABLED_ORGS (comma-separated IDs).
Disabled memory is never selected or written; inspection and erasure remain possible.

ATLAS_USER_MEMORY_ORG selects the installation organization (stable default:
"stable"; project mode: installation initial_org). ATLAS_USER_MEMORY_SECRET is the
internal service credential, falling back to the existing project identity or
stable proxy credential. The local stack generates an ephemeral credential if
none is configured. Missing authentication fails closed; anonymous chat has no
personal memory. The API-key branch must forward the verified key subject through
the same authenticated budget-identity boundary as WebUI.

The public result includes atlas.context_manifest.sources with kind=user_memory
and entry IDs for ordinary turns; project manifests contain no such source.
"What do you remember about me?" and explicit save/forget are answered directly
from storage. Empty, disabled, ambiguous and over-cap outcomes are explicit.
