# Project mode task B installation

This installs the PostgreSQL identity, scope and quota foundation defined by
`contracts/project-mode.md`. File lifecycle, browser identity, Work execution
and full migration manifests belong to subsequent tasks. This is not an
instruction to expose the legacy browser relay to multiple organizations.

1. Stop legacy project writers and back up their SQLite database. Select an
   organization name and the administrator's existing identity subject. There
   is no default organization, signup route or first-login administrator.
2. Prepare a JSON `Policy` document matching the frozen contract, with explicit
   quotas and installation ceilings. Organization administrators can tighten
   these ceilings. No unlimited storage or model spending is assumed.
3. Set `ATLAS_RETRIEVAL_DSN` to an operator connection with database superuser permission for explicit bootstrap
   (including the narrowly scoped security-definer authorization functions), using the installation's secret injection mechanism. Run:

   ```sh
   export PATH="$PWD/.venv/bin:$PATH"
   export ATLAS_PORT_BASE=8300
   make project-install PROJECT_INSTALL_ARGS='--name "Research collective" --admin-subject "configured-identity-subject" --policy /path/to/policy.json --legacy-db /path/to/projects.sqlite'
   ```

   Omit `--legacy-db` for an empty installation. The command prints only the
   new organization and member identifiers. Installation is one atomic SQL
   transaction and intentionally refuses an existing schema. It never changes
   the source SQLite database. All imported rows receive the selected initial
   organization and explicit administrator project grants; IDs, ordered turns,
   memory revision, stale context values and instruction text remain intact.
   Legacy child rows are retained behind RLS for the later migration adapters;
   reserved instruction/folder IDs do not claim file-version publication.
4. Configure database authentication for `atlas_project_app` and set the
   retrieval service's `ATLAS_PROJECT_DSN` to that non-owner role. Never use
   the operator DSN as the application DSN. Set a deployment-managed
   `ATLAS_PROJECT_SIGNING_KEY` of at least 32 bytes for the trusted identity
   broker and retrieval; do not put it in configuration tracked by Git.
5. The broker signs `project_identity.Identity` only after verifying host
   identity and resolving membership. Claims bind member, subject,
   organization, optional single project, audience, membership/policy revisions
   and at most 120 seconds of validity. The private retrieval header is
   `X-Atlas-Project-Identity`. It is not a public authentication alternative.
   Retrieval rechecks current database roles and grants on every request.
   The task-F BFF owns host session verification, CSRF and credential stripping.

Configuring `ATLAS_PROJECT_DSN` disables the legacy unscoped `/projects` HTTP
routes. Legacy deployment remains separate until explicitly installed. The
application never creates or migrates the schema on startup. Additional
organizations can be provisioned by the operator-only `provision` function;
organization administrators cannot provision organizations or inherit content
access. Role/policy changes invalidate older identity revisions; project grant
changes are checked directly even with a still-valid credential.

Task-B routes implement project creation/read/update, membership invitation and
activation/revocation, grant administration, policy and identity-bound list
snapshots. Other project routes authorize first and refuse with
`feature_not_available` pending their owning task. Project deletion requires the
later deletion job and approval implementation. Archive allows reads and Chat;
mutations and Work are denied. The generic chat transport is owned by the
orchestrator and remains a task-F/G integration responsibility.

Quota reservations serialize under an organization transaction lock. Calls to
`Scope.reserve` belong in the same transaction as the operation being admitted;
`Scope.reconcile` requires measured outcomes. Unknown dispatch keeps its
reservation. Reserve both daily and monthly model amounts; never release them
merely because an upstream request timed out. Active member/project counts and
initial instruction storage are checked on their production writes. File, job
and model dispatch adapters must use this boundary when those tasks ship.

Tests use the real PostgreSQL container from the local stack, or the same
`pgvector/pgvector:pg16` image on a random loopback port when run independently.
No provider credentials or GPU are required:

```sh
export PATH="$PWD/.venv/bin:$PATH"
export ATLAS_PORT_BASE=8300
python -m unittest tests.test_project_isolation tests.test_project_contracts
```
