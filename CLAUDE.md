# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A 7/24 autonomous pipeline that scrapes English automotive forums and technical sites, deduplicates the content, and generates Q/A pairs for LLM fine-tuning datasets. Data flows: **Scrape → Store → Deduplicate → Generate Q/A → repeat**.

## Running the pipeline

```bash
# Activate venv first
source venv/bin/activate

# Start infrastructure (PostgreSQL + Redis)
docker-compose up -d postgres redis

# Run a single round (best for testing)
python -m src.main single-round

# Continuous 7/24 mode
python -m src.main run

# Scrape one source only
python -m src.main scrape-only --source reddit_mechanicadvice

# Q/A generation only
python -m src.main generate-qa --batch-size 20

# Export Q/A pairs
python -m src.main export-qa --output training_data.jsonl --format jsonl

# Health / stats
python -m src.main health
python -m src.main stats

# Dashboard
python -m src.main dashboard   # http://localhost:5050
```

## Infrastructure

```bash
# Full Docker stack (no local Ollama)
docker-compose up -d

# Ollama is commented out in docker-compose.yml by default.
# Run it separately and set OLLAMA_URL env var, or uncomment the service.
ollama pull gemma3:4b    # primary Q/A model (configured in settings.yaml)
ollama pull gemma3:12b   # fallback / use if you have enough VRAM
```

Environment variables override `config/settings.yaml` credentials:
- `DATABASE_URL` — PostgreSQL DSN (e.g. `postgresql://collector:collector_pass@localhost:5432/car_collector`)
- `REDIS_URL` — Redis DSN (e.g. `redis://localhost:6379/0`)
- `OLLAMA_URL` — Ollama API URL (e.g. `http://localhost:11434`)
- `FLARESOLVERR_URL` — FlareSolverr API URL for cloudflare_protected sources (e.g. `http://localhost:8191`)

## Architecture

### Pipeline flow (`src/pipeline/__init__.py`)
`PipelineOrchestrator` is the single entry point. `initialize()` must be called before use; it connects the DB/Redis pool and syncs `sources` from `settings.yaml` into the `sources` table. Each round:
1. `run_scrape_round()` — fans out to all enabled sources with an `asyncio.Semaphore` (default 4 concurrent).
2. `run_dedup_round()` — post-round batch dedup pass.
3. `run_qa_round()` — only fires when `total_unique_documents >= min_documents_to_start` (default 100).

### Scraper hierarchy (`src/scrapers/`)
- `BaseScraper` — abstract base. Implements `fetch_page()` (aiohttp or Playwright), rate limiting via `AsyncLimiter`, retry via `tenacity`. Subclasses must implement `scrape_listing(url)` and `scrape_item(url)`. The `run()` async generator ties them together.
- `CloudflareBypass` (`bypass/cloudflare.py`) — wraps Playwright-stealth; persists per-source browser state to `data/cookies/<source_name>.json`. Used for `use_playwright` (non-Cloudflare) sources.
- `FlareSolverrClient` (`bypass/flaresolverr.py`) — sources with `cloudflare_protected: true` are fetched **exclusively** via a FlareSolverr service (no Playwright); one session is opened per source per round. If FlareSolverr is unreachable (`FLARESOLVERR_URL`), the source is skipped for that round with a warning.
- **Special scrapers** registered in `CUSTOM_SCRAPERS` dict in `pipeline/__init__.py`: `RedditAPIScraper` (uses Reddit JSON API) and `NHTSAAPIScraper` (uses NHTSA REST API).
- Anything not in `CUSTOM_SCRAPERS` falls back to `GenericForumScraper` or `GenericContentScraper`.

### Data models (`src/models/__init__.py`)
- `ScrapedItem` — raw output from scrapers (Pydantic).
- `StoredDocument` — what gets persisted to PostgreSQL.
- `QAPair` — fine-tuning output record.
- `SourceConfig` — parsed from YAML; passed to every scraper constructor.

### Deduplication (`src/dedup/__init__.py`)
3-layer check in `check_duplicate()`:
1. **Redis fast-path** — content hash and URL hash lookups.
2. **PostgreSQL exact match** — `content_hash` and `url_hash` columns.
3. **SimHash near-duplicate** — 128-bit SimHash with 4-bucket LSH; candidates from the same bucket are compared by Hamming distance; threshold configurable (default 0.85).

### Storage (`src/storage/__init__.py`)
`StorageManager.store_item()` is called per scraped item. It runs the dedup check inline, writes HTML to `data/raw/html/`, and inserts/updates the `scraped_data` table.

### Q/A generation (`src/qa_generator/__init__.py`)
Pulls unprocessed unique documents from PostgreSQL and sends them to Ollama (`gemma3:4b` by default, fallback `gemma3:12b`). Output is stored in `qa_pairs` table and written to `data/qa_output/` as JSONL.

### Database (`src/database/__init__.py`)
`DatabaseManager` — thin asyncpg connection-pool wrapper shared by all components. DSN resolved from `DATABASE_URL` env var or `config/settings.yaml`. Schema lives in `scripts/init-db.sql`.

### Cache (`src/cache/__init__.py`)
`RedisManager` — optional but strongly recommended. When unavailable, the pipeline degrades gracefully (dedup falls back to DB-only).

## Adding a new source

1. Add an entry under `sources:` in `config/settings.yaml`. Set `use_playwright: true` and `cloudflare_protected: true` if needed.
2. If the source needs custom parsing, create `src/scrapers/sources/<name>.py` inheriting `BaseScraper` and register it in `CUSTOM_SCRAPERS` in `src/pipeline/__init__.py`.
3. Otherwise the generic scraper uses the `selectors` dict from the YAML config.

## Key configuration knobs

| Setting | Location | Purpose |
|---|---|---|
| `general.max_workers` | settings.yaml | Concurrent scrapers |
| `general.round_delay_seconds` | settings.yaml | Wait between full rounds (default 43200 = 12h) |
| `pipeline.stages[generate_qa].min_documents_to_start` | settings.yaml | Q/A doesn't start until this many unique docs exist |
| `deduplication.similarity_threshold` | settings.yaml | SimHash near-dup threshold (0.0–1.0) |
| `quality.llm_scoring.min_score_to_keep` | settings.yaml | Minimum LLM quality score to retain a document |
| `qa_generator.batch_size` | settings.yaml | Documents per Q/A generation batch |
