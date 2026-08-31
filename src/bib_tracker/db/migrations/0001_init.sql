-- bib-tracker initial schema.
--
-- Two layers. poll_runs + snapshot_items are the OBSERVATION layer: append-only,
-- never edited, the ground truth of what each OPAC reported when. copies, loans
-- and renewals are the DERIVED layer, a materialisation of the lending history
-- that rebuild_history() can drop and recompute from the observations. Manual
-- corrections live in loan_overrides, outside the derived layer, keyed so they
-- survive a rebuild.
--
-- Note: this file must not open a transaction; the migration runner wraps it.

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE accounts (
    id              INTEGER PRIMARY KEY,
    -- Matches the attribute name in the NixOS module; the config join key.
    name            TEXT NOT NULL UNIQUE,
    library_type    TEXT NOT NULL,
    username        TEXT NOT NULL,
    base_url        TEXT,
    display_name    TEXT,
    colour          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    -- Soft delete: config may stop declaring an account, but its history stays.
    removed_at      TEXT,
    last_success_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (library_type, username)
);

CREATE TABLE poll_runs (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    trigger        TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual', 'startup', 'import')),
    -- 'suspect' means the scrape succeeded but the result looks like a parser
    -- break rather than reality; it is held back from reconciliation until a
    -- later poll confirms it.
    status         TEXT NOT NULL CHECK (status IN (
                       'running', 'success', 'suspect', 'auth_error',
                       'network_error', 'parse_error', 'internal_error')),
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
CREATE INDEX idx_poll_runs_account ON poll_runs(account_id, started_at DESC);
CREATE INDEX idx_poll_runs_pending ON poll_runs(account_id, reconciled, started_at);

CREATE TABLE snapshot_items (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES poll_runs(id) ON DELETE CASCADE,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    observed_at    TEXT NOT NULL,
    copy_key       TEXT NOT NULL,
    -- serialize_loan() output verbatim, so a future field is recoverable even
    -- if this schema does not extract it yet.
    raw_json       TEXT NOT NULL,
    title          TEXT NOT NULL,
    author         TEXT,
    publisher      TEXT,
    media_type     TEXT,
    item_id        TEXT,
    barcode        TEXT,
    call_number    TEXT,
    branch         TEXT,
    due_date       TEXT NOT NULL,
    checkout_date  TEXT,
    times_renewed  INTEGER NOT NULL DEFAULT 0,
    max_renewals   INTEGER,
    can_be_renewed INTEGER NOT NULL DEFAULT 1,
    isbn           TEXT,
    cover_url      TEXT,
    detail_url     TEXT
);
CREATE INDEX idx_snapshot_items_run ON snapshot_items(run_id);
CREATE INDEX idx_snapshot_items_key ON snapshot_items(account_id, copy_key, observed_at);

CREATE TABLE images (
    id         INTEGER PRIMARY KEY,
    sha256     TEXT NOT NULL,
    variant    TEXT NOT NULL CHECK (variant IN ('orig', 'sm', 'md', 'lg')),
    mime       TEXT NOT NULL,
    byte_size  INTEGER NOT NULL,
    width      INTEGER,
    height     INTEGER,
    source_url TEXT,
    provider   TEXT,
    fetched_at TEXT NOT NULL,
    data       BLOB NOT NULL,
    UNIQUE (sha256, variant)
);

CREATE TABLE media (
    id                    INTEGER PRIMARY KEY,
    media_key             TEXT NOT NULL UNIQUE,
    media_class           TEXT NOT NULL CHECK (media_class IN (
                              'book', 'audiobook', 'music', 'movie',
                              'game', 'magazine', 'other')),
    title                 TEXT NOT NULL,
    subtitle              TEXT,
    author                TEXT,
    author_key            TEXT,
    -- JSON array of the raw library media_type strings seen for this work.
    raw_media_types       TEXT NOT NULL DEFAULT '[]',
    isbn13                TEXT,
    isbn10                TEXT,
    ean                   TEXT,
    published_year        INTEGER,
    publisher             TEXT,
    page_count            INTEGER,
    language              TEXT,
    description           TEXT,
    cover_sha256          TEXT,
    cover_aspect          TEXT,
    effective_price_cents INTEGER,
    price_currency        TEXT NOT NULL DEFAULT 'EUR',
    price_basis           TEXT NOT NULL DEFAULT 'unknown' CHECK (price_basis IN (
                              'provider_list_price', 'manual', 'default_by_class', 'unknown')),
    metadata_state        TEXT NOT NULL DEFAULT 'pending' CHECK (metadata_state IN (
                              'pending', 'enriched', 'needs_confirmation', 'failed', 'skipped')),
    match_confidence      REAL,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Partial: most records have no ISBN, and those must not collide.
CREATE UNIQUE INDEX idx_media_isbn13 ON media(isbn13) WHERE isbn13 IS NOT NULL;
CREATE INDEX idx_media_class ON media(media_class);
CREATE INDEX idx_media_author_key ON media(author_key);

CREATE TABLE media_merges (
    id            INTEGER PRIMARY KEY,
    src_media_key TEXT NOT NULL,
    src_title     TEXT NOT NULL,
    dst_media_id  INTEGER NOT NULL REFERENCES media(id),
    reason        TEXT NOT NULL CHECK (reason IN ('isbn13', 'manual')),
    loans_moved   INTEGER NOT NULL,
    merged_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE copies (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    copy_key      TEXT NOT NULL,
    media_id      INTEGER NOT NULL REFERENCES media(id) ON DELETE RESTRICT,
    library_type  TEXT NOT NULL,
    item_id       TEXT,
    barcode       TEXT,
    call_number   TEXT,
    branch        TEXT,
    detail_url    TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE (account_id, copy_key)
);
CREATE INDEX idx_copies_media ON copies(media_id);

CREATE TABLE loans (
    id                   INTEGER PRIMARY KEY,
    -- copy_key @ opening run id. The run id is immutable observation-layer
    -- data, so this key is stable across a full rebuild -- which is what lets
    -- loan_overrides be re-applied afterwards.
    loan_key             TEXT NOT NULL UNIQUE,
    account_id           INTEGER NOT NULL REFERENCES accounts(id),
    copy_id              INTEGER NOT NULL REFERENCES copies(id) ON DELETE CASCADE,
    media_id             INTEGER NOT NULL REFERENCES media(id),
    state                TEXT NOT NULL CHECK (state IN ('open', 'returned')),

    lend_date            TEXT NOT NULL,
    lend_date_source     TEXT NOT NULL CHECK (lend_date_source IN (
                             'exact', 'due_minus_period', 'first_seen', 'before_tracking', 'manual')),
    -- Bounds are always recorded, so the point estimate above is never the
    -- only thing the UI and the statistics have to go on.
    lend_date_earliest   TEXT,
    lend_date_latest     TEXT,

    return_date          TEXT,
    return_date_source   TEXT CHECK (return_date_source IN ('exact', 'last_seen', 'midpoint', 'manual')),
    return_date_earliest TEXT,
    return_date_latest   TEXT,

    first_seen_run_id    INTEGER NOT NULL REFERENCES poll_runs(id),
    last_seen_run_id     INTEGER NOT NULL REFERENCES poll_runs(id),
    closing_run_id       INTEGER REFERENCES poll_runs(id),
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,

    first_due_date       TEXT NOT NULL,
    last_due_date        TEXT NOT NULL,
    times_renewed        INTEGER NOT NULL DEFAULT 0,
    max_renewals         INTEGER,
    can_be_renewed       INTEGER NOT NULL DEFAULT 0,
    -- Latched when observed: due dates move on renewal, so recomputing this
    -- later would quietly erase every overdue period.
    was_overdue          INTEGER NOT NULL DEFAULT 0,
    max_overdue_days     INTEGER NOT NULL DEFAULT 0,
    observation_count    INTEGER NOT NULL DEFAULT 1,
    derived_at           TEXT NOT NULL,

    duration_days INTEGER GENERATED ALWAYS AS (
        CASE WHEN return_date IS NULL THEN NULL
             ELSE CAST(julianday(return_date) - julianday(lend_date) AS INTEGER) END) VIRTUAL,
    duration_uncertainty_days INTEGER GENERATED ALWAYS AS (
        CAST(COALESCE(julianday(lend_date_latest)   - julianday(lend_date_earliest),   0)
           + COALESCE(julianday(return_date_latest) - julianday(return_date_earliest), 0)
            AS INTEGER)) VIRTUAL
);
CREATE INDEX idx_loans_media ON loans(media_id);
CREATE INDEX idx_loans_open ON loans(account_id, state);
CREATE INDEX idx_loans_lend ON loans(lend_date);
CREATE INDEX idx_loans_copy ON loans(copy_id, state);

CREATE TABLE loan_overrides (
    loan_key    TEXT PRIMARY KEY,
    lend_date   TEXT,
    return_date TEXT,
    state       TEXT CHECK (state IN ('open', 'returned')),
    media_id    INTEGER REFERENCES media(id),
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE renewals (
    id                   INTEGER PRIMARY KEY,
    loan_key             TEXT NOT NULL,
    observed_run_id      INTEGER NOT NULL REFERENCES poll_runs(id) ON DELETE CASCADE,
    observed_at          TEXT NOT NULL,
    due_date_before      TEXT,
    due_date_after       TEXT,
    times_renewed_before INTEGER,
    times_renewed_after  INTEGER,
    source               TEXT NOT NULL CHECK (source IN ('observed', 'app')),
    UNIQUE (loan_key, observed_run_id)
);

CREATE TABLE fees (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    fee_key           TEXT NOT NULL,
    description       TEXT NOT NULL,
    amount_cents      INTEGER NOT NULL,
    fee_date          TEXT,
    media_id          INTEGER REFERENCES media(id),
    first_seen_run_id INTEGER NOT NULL REFERENCES poll_runs(id),
    last_seen_run_id  INTEGER NOT NULL REFERENCES poll_runs(id),
    cleared_at        TEXT,
    UNIQUE (account_id, fee_key)
);

CREATE TABLE ratings (
    media_id   INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    rating     INTEGER CHECK (rating BETWEEN 1 AND 5),
    favourite  INTEGER NOT NULL DEFAULT 0,
    abandoned  INTEGER NOT NULL DEFAULT 0,
    -- Set when the user says "never ask me about this one again".
    dismissed_at TEXT,
    review     TEXT,
    rated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE metadata_records (
    id                  INTEGER PRIMARY KEY,
    media_id            INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,
    external_id         TEXT,
    external_url        TEXT,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'not_found', 'ambiguous', 'error')),
    match_method        TEXT CHECK (match_method IN ('isbn', 'ean', 'title_author', 'manual')),
    match_confidence    REAL,
    confirmed           INTEGER NOT NULL DEFAULT 0,
    title               TEXT,
    authors             TEXT,
    isbn13              TEXT,
    published_year      INTEGER,
    publisher           TEXT,
    page_count          INTEGER,
    language            TEXT,
    description         TEXT,
    -- Kept per provider and never merged: a single averaged number would hide
    -- that BGG rates out of 10 and Open Library out of 5.
    rating_value        REAL,
    rating_scale        REAL,
    rating_count        INTEGER,
    list_price_cents    INTEGER,
    list_price_currency TEXT,
    cover_source_url    TEXT,
    payload_json        TEXT,
    fetched_at          TEXT NOT NULL,
    refresh_after       TEXT,
    UNIQUE (media_id, provider)
);

CREATE TABLE price_estimates (
    id          INTEGER PRIMARY KEY,
    media_id    INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    confidence  TEXT NOT NULL CHECK (confidence IN ('exact', 'estimated', 'fallback')),
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (media_id, source)
);

CREATE TABLE enrichment_jobs (
    id              INTEGER PRIMARY KEY,
    media_id        INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed', 'skipped')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (media_id, provider)
);
CREATE INDEX idx_enrichment_jobs_ready ON enrichment_jobs(state, next_attempt_at);

CREATE TABLE http_cache (
    url_hash     TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    provider     TEXT,
    status       INTEGER NOT NULL,
    headers_json TEXT,
    body         BLOB,
    etag         TEXT,
    fetched_at   TEXT NOT NULL,
    expires_at   TEXT
);
CREATE INDEX idx_http_cache_expiry ON http_cache(expires_at);

-- Full-text search over the works, for the history filter bar.
CREATE VIRTUAL TABLE media_fts USING fts5(
    title, author, description,
    content = 'media',
    content_rowid = 'id',
    tokenize = 'porter unicode61'
);

CREATE TRIGGER media_fts_insert AFTER INSERT ON media BEGIN
    INSERT INTO media_fts(rowid, title, author, description)
    VALUES (new.id, new.title, new.author, new.description);
END;

CREATE TRIGGER media_fts_delete AFTER DELETE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, title, author, description)
    VALUES ('delete', old.id, old.title, old.author, old.description);
END;

CREATE TRIGGER media_fts_update AFTER UPDATE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, title, author, description)
    VALUES ('delete', old.id, old.title, old.author, old.description);
    INSERT INTO media_fts(rowid, title, author, description)
    VALUES (new.id, new.title, new.author, new.description);
END;

-- Every duration statistic reads this view, so the "what counts as reliable"
-- rule lives in exactly one place.
CREATE VIEW v_loan_durations AS
SELECT
    l.id,
    l.loan_key,
    l.account_id,
    l.media_id,
    l.state,
    COALESCE(o.lend_date, l.lend_date)     AS eff_lend_date,
    COALESCE(o.return_date, l.return_date) AS eff_return_date,
    l.lend_date_source,
    l.return_date_source,
    l.duration_uncertainty_days,
    CAST(julianday(COALESCE(o.return_date, l.return_date, date('now')))
       - julianday(COALESCE(o.lend_date, l.lend_date)) AS INTEGER) AS days_held,
    -- A loan already running when tracking started has no knowable start.
    CASE WHEN l.lend_date_source = 'before_tracking' THEN 1 ELSE 0 END AS lend_unreliable,
    m.media_class,
    m.title,
    m.author
FROM loans l
JOIN media m ON m.id = l.media_id
LEFT JOIN loan_overrides o ON o.loan_key = l.loan_key;

-- One row per completed loan with the price actually used, and where it came
-- from. Money statistics must always report that split rather than a bare sum.
CREATE VIEW v_money_saved AS
SELECT
    d.id,
    d.account_id,
    d.media_id,
    d.eff_lend_date,
    d.eff_return_date,
    d.media_class,
    m.effective_price_cents AS price_cents,
    m.price_basis,
    CASE WHEN m.price_basis IN ('provider_list_price', 'manual') THEN 1 ELSE 0 END AS price_is_exact
FROM v_loan_durations d
JOIN media m ON m.id = d.media_id
WHERE d.state = 'returned';
