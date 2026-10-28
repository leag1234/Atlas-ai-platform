-- REQ-KEY-01..06: SQLite K-A backend. Amounts are integer micro EUR, days UTC.
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, org_id TEXT NOT NULL, member_id TEXT NOT NULL,
 subject TEXT NOT NULL, budget_user TEXT NOT NULL, name TEXT NOT NULL,
 prefix TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
 hash_kind TEXT NOT NULL CHECK(hash_kind IN ('hmac-sha256','sha256-legacy')),
 scopes TEXT NOT NULL, expires_at INTEGER NOT NULL,
 daily_requests INTEGER NOT NULL CHECK(daily_requests > 0),
 daily_micro_eur INTEGER NOT NULL CHECK(daily_micro_eur > 0),
 created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
 last_used_at INTEGER, last_used_ip_prefix TEXT, revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS api_key_requests (
 id TEXT PRIMARY KEY, key_id TEXT NOT NULL REFERENCES api_keys(id),
 day TEXT NOT NULL, reserved INTEGER NOT NULL CHECK(reserved >= 0),
 charged INTEGER NOT NULL CHECK(charged >= 0),
 input_tokens INTEGER, output_tokens INTEGER,
 state TEXT NOT NULL CHECK(state IN ('reserved','complete','unknown'))
);
CREATE INDEX IF NOT EXISTS api_key_daily ON api_key_requests(key_id, day);
CREATE TABLE IF NOT EXISTS api_key_audit (
 id INTEGER PRIMARY KEY, event TEXT NOT NULL, key_id TEXT, timestamp INTEGER NOT NULL,
 detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_key_daily_summary (
 key_id TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL,
 charged_micro_eur INTEGER NOT NULL, PRIMARY KEY(key_id, day)
);
CREATE TABLE IF NOT EXISTS api_key_policy (
 org_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('off','admin_only','self_service')),
 daily_requests INTEGER NOT NULL CHECK(daily_requests > 0),
 daily_micro_eur INTEGER NOT NULL CHECK(daily_micro_eur > 0),
 max_lifetime_days INTEGER NOT NULL CHECK(max_lifetime_days BETWEEN 30 AND 365),
 active_key_limit INTEGER NOT NULL CHECK(active_key_limit > 0)
);
