-- 001_init.sql: initial schema, parity with the Django models it replaces.
-- Table and column names mirror Django's generated schema 1:1 so the
-- existing production database can be adopted in place.

-- sources ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles_source (
    id BIGSERIAL PRIMARY KEY,
    key VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    base_url VARCHAR(200) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    total_runs INTEGER NOT NULL DEFAULT 0,
    total_successes INTEGER NOT NULL DEFAULT 0,
    total_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_checked_at TIMESTAMPTZ NULL,
    last_success_at TIMESTAMPTZ NULL,
    circuit_open_until TIMESTAMPTZ NULL,
    last_error TEXT NOT NULL DEFAULT ''
);

-- journals -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles_journal (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(300) NOT NULL,
    issn VARCHAR(32) NOT NULL DEFAULT '',
    eissn VARCHAR(32) NOT NULL DEFAULT '',
    publisher VARCHAR(255) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS articles_journal_name_idx ON articles_journal (name);

-- articles -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles_article (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES articles_source (id) ON DELETE CASCADE,
    journal_id BIGINT NULL REFERENCES articles_journal (id) ON DELETE SET NULL,
    external_id VARCHAR(255) NOT NULL DEFAULT '',
    title VARCHAR(1000) NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    full_text TEXT NOT NULL DEFAULT '',
    language VARCHAR(32) NOT NULL DEFAULT '',
    publication_year INTEGER NULL,
    publication_date DATE NULL,
    url VARCHAR(1000) NOT NULL,
    doi VARCHAR(256) NOT NULL UNIQUE,
    local_md_path VARCHAR(512) NOT NULL DEFAULT '',
    volume VARCHAR(32) NOT NULL DEFAULT '',
    issue VARCHAR(32) NOT NULL DEFAULT '',
    pages VARCHAR(32) NOT NULL DEFAULT '',
    is_open_access BOOLEAN NOT NULL DEFAULT TRUE,
    is_retracted BOOLEAN NOT NULL DEFAULT FALSE,
    retraction_note TEXT NOT NULL DEFAULT '',
    cited_by_count INTEGER NOT NULL DEFAULT 0,
    is_peer_reviewed_or_refereed BOOLEAN NOT NULL DEFAULT FALSE,
    is_indexed_in_reputable_db BOOLEAN NOT NULL DEFAULT FALSE,
    has_doi_and_journal_card BOOLEAN NOT NULL DEFAULT FALSE,
    is_not_preprint_or_author_manuscript BOOLEAN NOT NULL DEFAULT FALSE,
    search_vector TEXT NOT NULL DEFAULT '',
    is_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    peer_review_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    indexing_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    doi_and_card_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    not_preprint_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    eligibility_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    peer_review_evidence TEXT NOT NULL DEFAULT '',
    indexing_evidence TEXT NOT NULL DEFAULT '',
    doi_journal_evidence TEXT NOT NULL DEFAULT '',
    preprint_evidence TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_article_url_idx ON articles_article (url);

-- authors ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles_author (
    id BIGSERIAL PRIMARY KEY,
    full_name VARCHAR(255) NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_author_full_name_idx ON articles_author (full_name);

CREATE TABLE IF NOT EXISTS articles_articleauthor (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES articles_article (id) ON DELETE CASCADE,
    author_id BIGINT NOT NULL REFERENCES articles_author (id) ON DELETE CASCADE,
    "order" INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT articles_articleauthor_unique_together UNIQUE (article_id, author_id, "order")
);

CREATE TABLE IF NOT EXISTS articles_identifier (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES articles_article (id) ON DELETE CASCADE,
    kind VARCHAR(64) NOT NULL,
    value VARCHAR(255) NOT NULL,
    CONSTRAINT articles_identifier_unique_together UNIQUE (article_id, kind, value)
);

-- search -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_searchjob (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query VARCHAR(512) NOT NULL,
    expression VARCHAR(1024) NOT NULL DEFAULT '',
    force_refresh_requested BOOLEAN NOT NULL DEFAULT FALSE,
    freshness_days_used INTEGER NOT NULL DEFAULT 14,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    stage VARCHAR(64) NOT NULL DEFAULT 'queued',
    substage VARCHAR(64) NOT NULL DEFAULT '',
    substage_label VARCHAR(128) NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    source_total INTEGER NOT NULL DEFAULT 0,
    source_done INTEGER NOT NULL DEFAULT 0,
    source_live INTEGER NOT NULL DEFAULT 0,
    source_failed JSONB NOT NULL DEFAULT '[]',
    source_timings JSONB NOT NULL DEFAULT '{}',
    index_hits_before INTEGER NOT NULL DEFAULT 0,
    index_hits_after INTEGER NOT NULL DEFAULT 0,
    rescan_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    rescan_reason VARCHAR(64) NOT NULL DEFAULT '',
    results JSONB NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    peer_reviewed_only BOOLEAN NOT NULL DEFAULT FALSE,
    indexed_only BOOLEAN NOT NULL DEFAULT FALSE,
    exclude_preprints BOOLEAN NOT NULL DEFAULT FALSE,
    exclude_retracted BOOLEAN NOT NULL DEFAULT FALSE,
    year_from INTEGER NULL,
    year_to INTEGER NULL,
    sort_by VARCHAR(16) NOT NULL DEFAULT 'relevance',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS search_searchjob_status_created_idx ON search_searchjob (status, created_at);
CREATE INDEX IF NOT EXISTS search_searchjob_query_created_idx ON search_searchjob (query, created_at);

CREATE TABLE IF NOT EXISTS search_searchwaitstat (
    id BIGSERIAL PRIMARY KEY,
    kind VARCHAR(64) NOT NULL UNIQUE,
    average_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS search_searchwaitstat_kind_idx ON search_searchwaitstat (kind);

-- extraction ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_articlequotes (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL UNIQUE REFERENCES articles_article (id) ON DELETE CASCADE,
    quotes JSONB NOT NULL DEFAULT '[]',
    tldr TEXT NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    extracted_at TIMESTAMPTZ NULL,
    model VARCHAR(128) NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS extraction_articlequotes_status_idx ON extraction_articlequotes (status);

-- ingestion ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_ingestionrun (
    id BIGSERIAL PRIMARY KEY,
    query VARCHAR(512) NOT NULL DEFAULT '',
    source_key VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'started',
    error TEXT NOT NULL DEFAULT '',
    fetched INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS ingestion_localimportfile (
    id BIGSERIAL PRIMARY KEY,
    path VARCHAR(1024) NOT NULL UNIQUE,
    sha256 VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    article_id BIGINT NULL REFERENCES articles_article (id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    last_seen_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS ingestion_exaapikeyquota (
    id BIGSERIAL PRIMARY KEY,
    api_key_id VARCHAR(64) NOT NULL UNIQUE,
    api_key_name VARCHAR(128) NOT NULL DEFAULT '',
    rate_limit_per_minute INTEGER NULL,
    usage_total_cost_usd NUMERIC(12, 4) NULL,
    usage_breakdown JSONB NOT NULL DEFAULT '[]',
    usage_window_start TIMESTAMPTZ NULL,
    usage_window_end TIMESTAMPTZ NULL,
    last_synced_at TIMESTAMPTZ NULL,
    last_error TEXT NOT NULL DEFAULT ''
);
