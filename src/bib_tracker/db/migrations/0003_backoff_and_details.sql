-- Poll back-off and the detail-lookup ledger.
--
-- The Stuttgart aDIS firewall answers 403 for hours after a burst of
-- requests. backoff_until / backoff_failures live on the account row
-- because they are facts about what the *site* did to *this account*, and
-- they must survive a rebuild of the derived layer.
--
-- detail_lookups is observation-style data like lookup_probes: one row per
-- copy whose detail page the poller asked the OPAC about, recording what
-- the detail page said (cover_url, isbn) and when. It gates the expensive
-- per-loan fetch_details request: a copy is only re-fetched after
-- details_retry_days, never on every poll.
--
-- Note: this file must not open a transaction; the migration runner wraps it.

ALTER TABLE accounts ADD COLUMN backoff_until TEXT;
ALTER TABLE accounts ADD COLUMN backoff_failures INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS detail_lookups (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    copy_key    TEXT    NOT NULL,
    cover_url   TEXT,
    isbn        TEXT,
    fetched_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, copy_key)
);
