-- =============================================================================
-- CAR DATA COLLECTOR BOT - PostgreSQL Database Schema
-- =============================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text similarity

-- =============================================================================
-- Sources Table - Track all configured data sources
-- =============================================================================
CREATE TABLE IF NOT EXISTS sources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(100) UNIQUE NOT NULL,
    source_type VARCHAR(50) NOT NULL,  -- forum, technical, qa
    base_url VARCHAR(500) NOT NULL,
    enabled BOOLEAN DEFAULT true,
    priority INTEGER DEFAULT 5,
    rate_limit_seconds DECIMAL(5,2) DEFAULT 5.0,
    scrape_interval_hours INTEGER DEFAULT 12,
    last_scrape_at TIMESTAMP WITH TIME ZONE,
    next_scrape_at TIMESTAMP WITH TIME ZONE,
    total_items_scraped INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- Scraped Data Table - Main content storage (partitioned by month)
-- =============================================================================
CREATE TABLE IF NOT EXISTS scraped_data (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    source_name VARCHAR(100) NOT NULL,
    page_url VARCHAR(2000) NOT NULL,
    url_hash VARCHAR(64) NOT NULL,  -- SHA256 of normalized URL
    content_hash VARCHAR(64) NOT NULL,  -- SHA256 of content
    simhash BIGINT,  -- For near-duplicate detection
    title VARCHAR(1000),
    content_text TEXT NOT NULL,
    content_html TEXT,
    author VARCHAR(200),
    category VARCHAR(50),  -- forum_thread, article, qa, technical
    quality_score DECIMAL(5,2),  -- 0-100
    language VARCHAR(10) DEFAULT 'en',
    word_count INTEGER,
    date_published TIMESTAMP WITH TIME ZONE,
    date_scraped TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_duplicate BOOLEAN DEFAULT false,
    duplicate_of UUID REFERENCES scraped_data(id),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
) PARTITION BY RANGE (created_at);

-- Create partitions for the next 12 months
DO $$
DECLARE
    start_date DATE;
    end_date DATE;
    partition_name TEXT;
BEGIN
    FOR i IN 0..12 LOOP
        start_date := DATE_TRUNC('month', CURRENT_DATE + (i || ' months')::INTERVAL);
        end_date := start_date + INTERVAL '1 month';
        partition_name := 'scraped_data_' || TO_CHAR(start_date, 'YYYY_MM');
        
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF scraped_data
             FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_date, end_date
        );
    END LOOP;
END $$;

-- =============================================================================
-- Scrape Logs Table - Track each scraping session
-- =============================================================================
CREATE TABLE IF NOT EXISTS scrape_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    source_name VARCHAR(100) NOT NULL,
    round_number INTEGER NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(20) DEFAULT 'running',  -- running, completed, failed, cancelled
    pages_scraped INTEGER DEFAULT 0,
    items_scraped INTEGER DEFAULT 0,
    items_stored INTEGER DEFAULT 0,
    duplicates_found INTEGER DEFAULT 0,
    errors_count INTEGER DEFAULT 0,
    error_messages JSONB DEFAULT '[]',
    duration_seconds DECIMAL(10,2),
    metadata JSONB DEFAULT '{}'
);

-- =============================================================================
-- Quality Scores Table - LLM-based quality assessment
-- =============================================================================
CREATE TABLE IF NOT EXISTS quality_scores (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scraped_data_id UUID REFERENCES scraped_data(id) ON DELETE CASCADE,
    overall_score DECIMAL(5,2),
    relevance_score DECIMAL(5,2),  -- How relevant to automotive
    technical_score DECIMAL(5,2),  -- Technical accuracy
    completeness_score DECIMAL(5,2),  -- Information completeness
    readability_score DECIMAL(5,2),
    is_spam BOOLEAN DEFAULT false,
    is_low_quality BOOLEAN DEFAULT false,
    llm_model VARCHAR(100),
    evaluated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    raw_response JSONB
);

-- =============================================================================
-- QA Pairs Table - Generated Q&A for fine-tuning
-- =============================================================================
CREATE TABLE IF NOT EXISTS qa_pairs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scraped_data_id UUID REFERENCES scraped_data(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    category VARCHAR(50),  -- repair, maintenance, specs, troubleshooting
    quality_score DECIMAL(5,2),
    llm_model VARCHAR(100),
    generated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_verified BOOLEAN DEFAULT false,
    metadata JSONB DEFAULT '{}'
);

-- =============================================================================
-- URL Visited Table - Track visited URLs to avoid re-scraping
-- =============================================================================
CREATE TABLE IF NOT EXISTS urls_visited (
    url_hash VARCHAR(64) PRIMARY KEY,
    url VARCHAR(2000) NOT NULL,
    source_name VARCHAR(100) NOT NULL,
    first_visited_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_visited_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    visit_count INTEGER DEFAULT 1,
    last_status VARCHAR(20),  -- success, error, blocked
    content_changed BOOLEAN DEFAULT false
);

-- =============================================================================
-- Indexes for Performance
-- =============================================================================

-- scraped_data indexes
CREATE INDEX IF NOT EXISTS idx_scraped_data_source_id ON scraped_data(source_id);
CREATE INDEX IF NOT EXISTS idx_scraped_data_source_name ON scraped_data(source_name);
CREATE INDEX IF NOT EXISTS idx_scraped_data_url_hash ON scraped_data(url_hash);
CREATE INDEX IF NOT EXISTS idx_scraped_data_content_hash ON scraped_data(content_hash);
CREATE INDEX IF NOT EXISTS idx_scraped_data_simhash ON scraped_data(simhash);
CREATE INDEX IF NOT EXISTS idx_scraped_data_quality ON scraped_data(quality_score) WHERE quality_score IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_scraped_data_created ON scraped_data(created_at);
CREATE INDEX IF NOT EXISTS idx_scraped_data_not_duplicate ON scraped_data(id) WHERE is_duplicate = false;

-- Full-text search index
CREATE INDEX IF NOT EXISTS idx_scraped_data_title_fts ON scraped_data USING gin(to_tsvector('english', title));
CREATE INDEX IF NOT EXISTS idx_scraped_data_content_fts ON scraped_data USING gin(to_tsvector('english', content_text));

-- scrape_logs indexes
CREATE INDEX IF NOT EXISTS idx_scrape_logs_source ON scrape_logs(source_id);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_status ON scrape_logs(status);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_started ON scrape_logs(started_at);

-- qa_pairs indexes
CREATE INDEX IF NOT EXISTS idx_qa_pairs_scraped_data ON qa_pairs(scraped_data_id);
CREATE INDEX IF NOT EXISTS idx_qa_pairs_category ON qa_pairs(category);
CREATE INDEX IF NOT EXISTS idx_qa_pairs_quality ON qa_pairs(quality_score);

-- urls_visited indexes
CREATE INDEX IF NOT EXISTS idx_urls_visited_source ON urls_visited(source_name);
CREATE INDEX IF NOT EXISTS idx_urls_visited_last ON urls_visited(last_visited_at);

-- =============================================================================
-- Functions
-- =============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_sources_updated_at
    BEFORE UPDATE ON sources
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Function to create new monthly partitions
CREATE OR REPLACE FUNCTION create_monthly_partition()
RETURNS void AS $$
DECLARE
    next_month DATE;
    partition_name TEXT;
BEGIN
    next_month := DATE_TRUNC('month', CURRENT_DATE + INTERVAL '1 month');
    partition_name := 'scraped_data_' || TO_CHAR(next_month, 'YYYY_MM');
    
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF scraped_data
         FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        next_month,
        next_month + INTERVAL '1 month'
    );
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- Initial Source Data (English Automotive Sources)
-- =============================================================================
INSERT INTO sources (name, source_type, base_url, enabled, priority, rate_limit_seconds, scrape_interval_hours) VALUES
    -- Forums
    ('reddit_mechanicadvice', 'forum', 'https://www.reddit.com/r/MechanicAdvice', true, 1, 2.0, 6),
    ('reddit_cartalk', 'forum', 'https://www.reddit.com/r/Cartalk', true, 1, 2.0, 6),
    ('bobistheoilguy', 'forum', 'https://bobistheoilguy.com', true, 2, 5.0, 12),
    ('automotiveforums', 'forum', 'https://www.automotiveforums.com', true, 3, 5.0, 12),
    ('cargurus_forum', 'forum', 'https://www.cargurus.com/Cars/Discussion', true, 2, 5.0, 12),
    
    -- Technical Sites
    ('repairpal', 'technical', 'https://repairpal.com', true, 1, 5.0, 24),
    ('carcarekiosk', 'technical', 'https://www.carcarekiosk.com', true, 2, 5.0, 24),
    ('autozone_guides', 'technical', 'https://www.autozone.com/diy', true, 2, 5.0, 24),
    
    -- Complaint/Issue Sites
    ('carcomplaints', 'technical', 'https://www.carcomplaints.com', true, 1, 3.0, 48),
    ('nhtsa', 'technical', 'https://www.nhtsa.gov/recalls-complaints', true, 1, 3.0, 48),
    
    -- Specs & Reviews
    ('motortrend', 'technical', 'https://www.motortrend.com', true, 3, 5.0, 48),
    ('caranddriver', 'technical', 'https://www.caranddriver.com', true, 3, 5.0, 48),
    
    -- Q&A Sites
    ('2carpros', 'qa', 'https://www.2carpros.com', true, 1, 5.0, 12)
ON CONFLICT (name) DO NOTHING;
