# REQ-PROJ-05: expired Work lease recovery

Owner mandate: 2026-10-29, bounded work-lease-recovery task.

An unreleased lease becomes recoverable 15 seconds after expires_at. Recovery
holds the organization transaction lock and rechecks expiry under the lease row
lock. Renewals committed before that check exclude the lease. Concurrent sweeps
are idempotent. Organization reservation checks run recovery before checking
concurrent_jobs; the existing private scheduler tick also sweeps, even when no
scheduled task is due.

Recovery atomically marks the session and its running turn failed, appends an
interruption explanation to the turn, invalidates pending approvals, releases
the lease and records work_lease_expired in the audit table. Shared terminal
reservation settlement releases concurrent_jobs, stored_bytes and temporary_bytes.
Previously settled model amounts remain settled; reserved model amounts become
unknown without reducing the amount. Absence of a final usage report never proves
zero spend. The five reservation rows must exist or the transaction fails.

Maintenance can recover private sessions and revoked holders without exposing
content. The security-definer function accepts the authenticated organization for
lazy recovery; its NULL form is called by the authenticated private scheduler for
all organizations. It returns a count only. No public HTTP recovery endpoint is
added. Normal terminal reports retain their authorization, cleanup and spend checks.
Expiry recovery fences further broker operations; it does not certify physical
workspace or remote process deletion after a holder crash.

Deploy through the existing operator-only upgrade_work migration before deploying
these callers. The work schema installs PostgreSQL's bundled uuid-ossp extension
in public to reproduce the existing Python UUID-v5 reservation identifiers, including
leases created before this change. This avoids rewriting old reservation IDs or
adding a second accounting format. The supported PostgreSQL image includes it;
no new application package, service or live provider is required. Rollback callers
first; the additive functions and extension may safely remain installed.
