-- Price provenance and the shop-probe ledger.
--
-- media.price_provider records *which* provider stated the effective price,
-- so the UI can name the source instead of only the basis word.
--
-- lookup_probes is observation-style data: what one provider said about one
-- work, once. It is NOT derived — nothing rebuilds it. The UNIQUE constraint
-- is the "don't retry a price lookup all the time" mechanism: one row per
-- provider per purpose per work; a probed provider is skipped until the row
-- is deleted or, for blocked/error only, older than price_retry_days.
--
-- Note: this file must not open a transaction; the migration runner wraps it.

ALTER TABLE media ADD COLUMN price_provider TEXT;

CREATE TABLE IF NOT EXISTS lookup_probes (
    media_id      INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    provider      TEXT    NOT NULL,
    attempted_for TEXT    NOT NULL CHECK (attempted_for IN ('price', 'cover')),
    outcome       TEXT    NOT NULL CHECK (outcome IN ('found', 'not_found', 'blocked', 'error')),
    checked_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    detail        TEXT,
    UNIQUE (media_id, provider, attempted_for)
);
