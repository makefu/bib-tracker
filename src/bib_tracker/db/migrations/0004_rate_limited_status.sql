-- Widen the poll-run status CHECK for 'rate_limited'.
--
-- A 403/429 from an OPAC is a different fact from a network error: one says
-- "be silent for hours", the other "try again on the next tick". The scheduler
-- needs them apart, so they get separate statuses. SQLite has no ALTER
-- CONSTRAINT, so poll_runs is rebuilt the documented way — create, copy, drop,
-- rename. No triggers or views attach to poll_runs, and the child tables keep
-- their REFERENCES clause through the rename (verified by test_migrations).
--
-- Note: this file must not open a transaction; the migration runner wraps it.

CREATE TABLE poll_runs_new (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    trigger        TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual', 'startup', 'import')),
    -- 'suspect' means the scrape succeeded but the result looks like a parser
    -- break rather than reality; it is held back from reconciliation until a
    -- later poll confirms it.
    -- 'rate_limited' means the site answered 403/429: the account goes quiet
    -- for a back-off window rather than retrying on the normal schedule.
    status         TEXT NOT NULL CHECK (status IN (
                       'running', 'success', 'suspect', 'auth_error',
                       'network_error', 'parse_error', 'internal_error',
                       'rate_limited')),
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    duration_ms    INTEGER,
    loan_count     INTEGER,
    fee_count      INTEGER,
    fees_supported INTEGER NOT NULL DEFAULT 1,
    suspect_reason TEXT,
    error_kind     TEXT,
    error_message  TEXT,
    reconciled     INTEGER NOT NULL DEFAULT 0
);

INSERT INTO poll_runs_new SELECT * FROM poll_runs;

DROP TABLE poll_runs;

ALTER TABLE poll_runs_new RENAME TO poll_runs;

CREATE INDEX idx_poll_runs_account ON poll_runs(account_id, started_at DESC);
CREATE INDEX idx_poll_runs_pending ON poll_runs(account_id, reconciled, started_at);
