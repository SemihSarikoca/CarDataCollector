-- Migration: add compound indexes for common query patterns.
-- Safe to run on a live database; uses IF NOT EXISTS / CONCURRENTLY.
-- Run with:
--   psql "$DATABASE_URL" -f scripts/add_compound_indexes.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scraped_source_date
    ON scraped_data(source_name, date_scraped DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scraped_source_unique
    ON scraped_data(source_name)
    WHERE is_duplicate = false;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scraped_dup_qa
    ON scraped_data(is_duplicate, qa_count);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scrape_logs_round_src
    ON scrape_logs(round_number, source_name);
