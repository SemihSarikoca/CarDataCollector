-- =============================================================================
-- CAR DATA COLLECTOR BOT - PostgreSQL Database Schema
-- =============================================================================
-- Stand-alone (un-partitioned) schema. Add native partitioning or pg_partman
-- later when data volume warrants it; the current schema indexes are enough
-- for tens of millions of rows.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- =============================================================================
-- Sources - Configured data sources (mirrored from settings.yaml at startup)
-- =============================================================================
CREATE TABLE IF NOT EXISTS sources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(100) UNIQUE NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    enabled BOOLEAN DEFAULT true,
    priority INTEGER DEFAULT 5,
    rate_limit_seconds NUMERIC(5,2) DEFAULT 5.0,
    scrape_interval_hours INTEGER DEFAULT 12,
    last_scrape_at TIMESTAMPTZ,
    next_scrape_at TIMESTAMPTZ,
    total_items_scraped INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- Scraped Data - Main content metadata. Body text is stored on disk; this
-- table holds metadata + a content excerpt used for indexing/search.
-- =============================================================================
CREATE TABLE IF NOT EXISTS scraped_data (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id UUID REFERENCES sources(id) ON DELETE SET NULL,
    source_name VARCHAR(100) NOT NULL,
    source_url VARCHAR(2000) DEFAULT '',
    page_url VARCHAR(2000) NOT NULL,
    url_hash VARCHAR(64) UNIQUE NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    simhash VARCHAR(64) DEFAULT '',
    title VARCHAR(1000) DEFAULT '',
    content_text TEXT DEFAULT '',
    content_html TEXT,
    author VARCHAR(200) DEFAULT '',
    category VARCHAR(50) DEFAULT 'article',
    quality_score NUMERIC(5,2),
    language VARCHAR(10) DEFAULT 'en',
    word_count INTEGER DEFAULT 0,
    content_length INTEGER DEFAULT 0,
    file_size_bytes BIGINT DEFAULT 0,
    file_path_html VARCHAR(2000) DEFAULT '',
    file_path_pdf VARCHAR(2000) DEFAULT '',
    pipeline_stage VARCHAR(30) DEFAULT 'scraped',
    is_duplicate BOOLEAN DEFAULT false,
    duplicate_of UUID REFERENCES scraped_data(id) ON DELETE SET NULL,
    qa_count INTEGER DEFAULT 0,
    round_number INTEGER DEFAULT 0,
    tags TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}'::jsonb,
    date_published TIMESTAMPTZ,
    date_scraped TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    date_processed TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- Q/A Pairs
-- =============================================================================
CREATE TABLE IF NOT EXISTS qa_pairs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID REFERENCES scraped_data(id) ON DELETE CASCADE,
    source_name VARCHAR(100) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    qa_hash VARCHAR(64) UNIQUE NOT NULL,
    category VARCHAR(50) DEFAULT '',
    car_brand VARCHAR(80) DEFAULT '',
    car_model VARCHAR(80) DEFAULT '',
    car_year VARCHAR(8) DEFAULT '',
    quality_score NUMERIC(5,3) DEFAULT 0,
    llm_model VARCHAR(100) DEFAULT '',
    is_verified BOOLEAN DEFAULT false,
    metadata JSONB DEFAULT '{}'::jsonb,
    date_generated TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- Visited URLs - Skip-list / change detection
-- =============================================================================
CREATE TABLE IF NOT EXISTS urls_visited (
    url_hash VARCHAR(64) PRIMARY KEY,
    url VARCHAR(2000) NOT NULL,
    source_name VARCHAR(100) NOT NULL,
    first_visited_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_visited_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    visit_count INTEGER DEFAULT 1,
    last_content_hash VARCHAR(64) DEFAULT '',
    has_changed BOOLEAN DEFAULT false,
    last_status VARCHAR(20) DEFAULT 'success'
);

-- =============================================================================
-- Scrape Logs - Per (round, source) execution log
-- =============================================================================
CREATE TABLE IF NOT EXISTS scrape_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id UUID REFERENCES sources(id) ON DELETE SET NULL,
    source_name VARCHAR(100) NOT NULL,
    round_number INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'running',
    pages_scraped INTEGER DEFAULT 0,
    items_scraped INTEGER DEFAULT 0,
    items_stored INTEGER DEFAULT 0,
    duplicates_found INTEGER DEFAULT 0,
    errors_count INTEGER DEFAULT 0,
    error_messages JSONB DEFAULT '[]'::jsonb,
    duration_seconds NUMERIC(10,2),
    metadata JSONB DEFAULT '{}'::jsonb
);

-- =============================================================================
-- Round Summaries - One row per pipeline round
-- =============================================================================
CREATE TABLE IF NOT EXISTS rounds (
    round_number INTEGER PRIMARY KEY,
    start_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMPTZ,
    total_urls_visited INTEGER DEFAULT 0,
    total_items_scraped INTEGER DEFAULT 0,
    total_items_stored INTEGER DEFAULT 0,
    total_duplicates_found INTEGER DEFAULT 0,
    total_qa_generated INTEGER DEFAULT 0,
    errors_count INTEGER DEFAULT 0,
    sources_completed JSONB DEFAULT '[]'::jsonb,
    status VARCHAR(20) DEFAULT 'running'
);

-- =============================================================================
-- SimHash Index - bucketed for cheap candidate lookup
-- =============================================================================
CREATE TABLE IF NOT EXISTS simhash_index (
    document_id UUID PRIMARY KEY REFERENCES scraped_data(id) ON DELETE CASCADE,
    simhash_value VARCHAR(64) NOT NULL,
    bucket_0 VARCHAR(16) DEFAULT '',
    bucket_1 VARCHAR(16) DEFAULT '',
    bucket_2 VARCHAR(16) DEFAULT '',
    bucket_3 VARCHAR(16) DEFAULT ''
);

-- =============================================================================
-- Quality Scores
-- =============================================================================
CREATE TABLE IF NOT EXISTS quality_scores (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scraped_data_id UUID REFERENCES scraped_data(id) ON DELETE CASCADE,
    overall_score NUMERIC(5,2),
    relevance_score NUMERIC(5,2),
    technical_score NUMERIC(5,2),
    completeness_score NUMERIC(5,2),
    readability_score NUMERIC(5,2),
    is_spam BOOLEAN DEFAULT false,
    is_low_quality BOOLEAN DEFAULT false,
    llm_model VARCHAR(100),
    evaluated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    raw_response JSONB
);

-- =============================================================================
-- Indexes
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_scraped_source         ON scraped_data(source_name);
CREATE INDEX IF NOT EXISTS idx_scraped_url_hash       ON scraped_data(url_hash);
CREATE INDEX IF NOT EXISTS idx_scraped_content_hash   ON scraped_data(content_hash);
CREATE INDEX IF NOT EXISTS idx_scraped_simhash        ON scraped_data(simhash);
CREATE INDEX IF NOT EXISTS idx_scraped_stage          ON scraped_data(pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_scraped_dup            ON scraped_data(is_duplicate);
CREATE INDEX IF NOT EXISTS idx_scraped_round          ON scraped_data(round_number);
CREATE INDEX IF NOT EXISTS idx_scraped_date_scraped   ON scraped_data(date_scraped DESC);
CREATE INDEX IF NOT EXISTS idx_scraped_qa_count       ON scraped_data(qa_count);
CREATE INDEX IF NOT EXISTS idx_scraped_title_trgm     ON scraped_data USING gin(title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_scraped_qa_pending     ON scraped_data(date_scraped DESC)
    WHERE is_duplicate = false AND qa_count = 0;

CREATE INDEX IF NOT EXISTS idx_qa_doc                 ON qa_pairs(document_id);
CREATE INDEX IF NOT EXISTS idx_qa_source              ON qa_pairs(source_name);
CREATE INDEX IF NOT EXISTS idx_qa_brand               ON qa_pairs(car_brand);
CREATE INDEX IF NOT EXISTS idx_qa_date                ON qa_pairs(date_generated DESC);

CREATE INDEX IF NOT EXISTS idx_visited_source         ON urls_visited(source_name);
CREATE INDEX IF NOT EXISTS idx_visited_changed        ON urls_visited(has_changed) WHERE has_changed = true;

CREATE INDEX IF NOT EXISTS idx_scrape_logs_round      ON scrape_logs(round_number);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_source     ON scrape_logs(source_id);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_started    ON scrape_logs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_simhash_b0             ON simhash_index(bucket_0);
CREATE INDEX IF NOT EXISTS idx_simhash_b1             ON simhash_index(bucket_1);
CREATE INDEX IF NOT EXISTS idx_simhash_b2             ON simhash_index(bucket_2);
CREATE INDEX IF NOT EXISTS idx_simhash_b3             ON simhash_index(bucket_3);

-- Compound indexes for common multi-column query patterns
CREATE INDEX IF NOT EXISTS idx_scraped_source_date    ON scraped_data(source_name, date_scraped DESC);
CREATE INDEX IF NOT EXISTS idx_scraped_source_unique  ON scraped_data(source_name) WHERE is_duplicate = false;
CREATE INDEX IF NOT EXISTS idx_scraped_dup_qa         ON scraped_data(is_duplicate, qa_count);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_round_src  ON scrape_logs(round_number, source_name);

-- =============================================================================
-- Functions and triggers
-- =============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_sources_updated_at ON sources;
CREATE TRIGGER update_sources_updated_at
    BEFORE UPDATE ON sources
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_scraped_updated_at ON scraped_data;
CREATE TRIGGER update_scraped_updated_at
    BEFORE UPDATE ON scraped_data
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- =============================================================================
-- Initial source seeding (idempotent; pipeline also upserts at startup)
-- =============================================================================
INSERT INTO sources (name, source_type, base_url, enabled, priority, rate_limit_seconds, scrape_interval_hours) VALUES
    ('reddit_mechanicadvice', 'forum',     'https://www.reddit.com/r/MechanicAdvice', true, 1, 2.0, 6),
    ('reddit_cartalk',         'forum',     'https://www.reddit.com/r/Cartalk',        true, 1, 2.0, 6),
    ('bobistheoilguy',         'forum',     'https://bobistheoilguy.com',              true, 2, 5.0, 12),
    ('automotiveforums',       'forum',     'https://www.automotiveforums.com',        true, 3, 5.0, 12),
    ('cargurus_forum',         'forum',     'https://www.cargurus.com',                true, 2, 5.0, 12),
    ('repairpal',              'technical', 'https://repairpal.com',                   true, 1, 5.0, 24),
    ('carcarekiosk',           'technical', 'https://www.carcarekiosk.com',            true, 2, 5.0, 24),
    ('autozone_guides',        'technical', 'https://www.autozone.com',                true, 2, 5.0, 24),
    ('carcomplaints',          'technical', 'https://www.carcomplaints.com',           true, 1, 3.0, 48),
    ('nhtsa',                  'technical', 'https://api.nhtsa.gov',                   true, 1, 1.0, 168),
    ('motortrend',             'content',   'https://www.motortrend.com',              true, 3, 5.0, 48),
    ('caranddriver',           'content',   'https://www.caranddriver.com',            true, 3, 5.0, 48),
    ('2carpros',               'qa',        'https://www.2carpros.com',                true, 1, 5.0, 12)
ON CONFLICT (name) DO NOTHING;
