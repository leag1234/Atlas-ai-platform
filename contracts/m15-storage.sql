-- SQLite local, migrations réversibles : supprimer les tables en ordre inverse.
-- Montants entiers en micro-euros ; réservation non soldée reste débitée.
CREATE TABLE developers (
  id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  daily_requests INTEGER NOT NULL CHECK (daily_requests > 0),
  daily_micro_eur INTEGER NOT NULL CHECK (daily_micro_eur > 0)
);
CREATE TABLE requests (
  id TEXT PRIMARY KEY,
  developer TEXT NOT NULL REFERENCES developers(id),
  day TEXT NOT NULL,
  reserved INTEGER NOT NULL CHECK (reserved BETWEEN 1 AND 50000),
  charged INTEGER NOT NULL CHECK (charged >= 0),
  input_tokens INTEGER,
  output_tokens INTEGER,
  state TEXT NOT NULL CHECK (state IN ('reserved','complete','unknown'))
);
CREATE INDEX requests_quota ON requests(developer, day);
