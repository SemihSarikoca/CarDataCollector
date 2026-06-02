# 🚗 Car Data Collector Bot

English automotive forums and technical sites data collection system for LLM fine-tuning dataset preparation.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       PIPELINE ORCHESTRATOR                              │
│                      (Continuous 7/24 Operation)                         │
├─────────────┬────────────────┬─────────────────┬─────────────────────────┤
│   SCRAPE    │     STORE      │   DEDUPLICATE   │     GENERATE Q/A        │
│             │                │                 │                         │
│ ┌─────────┐ │ ┌────────────┐ │ ┌─────────────┐ │ ┌─────────────────────┐ │
│ │Reddit   │ │ │PostgreSQL  │ │ │SHA-256 Hash │ │ │Gemma 12B            │ │
│ │BITOG    │ │ │HTML Files  │ │ │SimHash      │ │ │   or                │ │
│ │RepairPal│ │ │Redis Cache │ │ │Quality LLM  │ │ │Qwen 2.5 14B         │ │
│ │2CarPros │ │ └────────────┘ │ └─────────────┘ │ │  (via Ollama)       │ │
│ │CarGurus │ │                │                 │ └─────────────────────┘ │
│ │NHTSA    │ │                │                 │                         │
│ │AutoZone │ │                │                 │  Output:                │
│ │+7 more  │ │                │                 │  JSON Q/A pairs         │
│ └─────────┘ │                │                 │  (fine-tuning ready)    │
├─────────────┴────────────────┴─────────────────┴─────────────────────────┤
│                        PostgreSQL Database                               │
│         scraped_data | qa_pairs | sources | scrape_logs                  │
├──────────────────────────────────────────────────────────────────────────┤
│                         File System Storage                              │
│              /data/car-collector/raw/html/  (TBs of data)                │
│              /data/car-collector/qa_output/ (JSONL)                      │
├──────────────────────────────────────────────────────────────────────────┤
│                         Flask Dashboard (:5050)                          │
│         Monitoring, stats, data browser, pipeline start/stop             │
└──────────────────────────────────────────────────────────────────────────┘
```

## Features

- **13+ English sources** — forums, technical sites, Q&A platforms, official databases
- **Playwright + Cloudflare bypass** — JS rendering, stealth mode, anti-detection
- **PostgreSQL + Redis** — scalable storage for TB-level data
- **3-layer duplicate detection** — SHA-256 + SimHash + LLM quality scoring
- **Intelligent Q/A generation** — Gemma/Qwen English Q&A pairs
- **7/24 continuous operation** — configurable intervals (default 12h per source)
- **Real-time Dashboard** — Flask web UI for monitoring
- **Docker ready** — easy deployment, auto-restart

## Sources (13+)

| # | Source | Type | Description |
|---|--------|------|-------------|
| 1 | Reddit r/MechanicAdvice | Forum | 1.2M+ members, active Q&A |
| 2 | Reddit r/Cartalk | Forum | Technical discussions |
| 3 | Bob Is The Oil Guy | Forum | Oil & maintenance expertise |
| 4 | Automotive Forums | Forum | Multi-brand discussions |
| 5 | CarGurus Forum | Forum | Car discussions & Q&A |
| 6 | RepairPal | Technical | Repair guides & estimates |
| 7 | Car Care Kiosk | Technical | How-to guides |
| 8 | AutoZone Guides | Technical | DIY repair manuals |
| 9 | CarComplaints | Complaints | TSBs & known issues |
| 10 | NHTSA | Complaints | Official US complaints database |
| 11 | Motor Trend | Content | Specs & reviews |
| 12 | Car and Driver | Content | Technical articles |
| 13 | 2CarPros | Q&A | Professional mechanic Q&A |

## 🚀 Getting Started (From Scratch)

This step-by-step guide will walk you through setting up the Data Collector Bot from absolute scratch.

### Prerequisites

Before you begin, ensure you have the following installed on your system (Server or Local Machine):
- **Git**: To clone the repository.
- **Python 3.11+**: If you plan to run the bot outside of Docker.
- **Docker & Docker Compose**: Required for running PostgreSQL, Redis, and optionally the bot itself safely.
- **Ollama**: Required for generating Q/A datasets via local LLMs.

---

### Option A: Complete Docker Setup (Recommended)

This is the easiest and most reliable way to run the entire data collection pipeline (Database, Cache, and the Bot).

**Step 1: Clone the Repository**
```bash
git clone <your-repo-url>
cd datacollectorbot
```

**Step 2: Prepare the Data Storage Directory**
The bot generates TBs of scraped HTML files and JSON datasets. You must set up a dedicated folder (preferably on a large external disk).
```bash
# Create the main storage directory
sudo mkdir -p /data/car-collector

# Grant ownership to your current user
sudo chown -R $USER:$USER /data/car-collector
```
*(If you are using a secondary drive, mount it to `/data/car-collector` before running this).*

**Step 3: Review Configuration**
All project settings (intervals, rate limits, DB credentials) live in the `settings.yaml` file.
```bash
nano config/settings.yaml
```

**Step 4: Launch the Services**
Start everything in the background using Docker Compose. The Postgres database schema will be automatically generated using `scripts/init-db.sql`.
```bash
docker-compose up -d
```

**Step 5: Monitor the Bot**
- **Dashboard:** Open `http://localhost:5001` (mapped from container port 5050) to view real-time stats and control the pipeline.
- **Container Logs:** Watch what the collector is doing in real-time:
  ```bash
  docker-compose logs -f collector
  ```

---

### Option B: Local Development Setup

Use this method if you plan to write code, debug scrapers, or prefer running the Python scripts natively.

**Step 1: Clone and Set Up Virtual Environment**
```bash
git clone <your-repo-url>
cd datacollectorbot

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install all Python dependencies
pip install -r requirements.txt
```

**Step 2: Install Playwright Sub-dependencies**
The bot utilizes Playwright for JS execution and Cloudflare evasion.
```bash
playwright install chromium
```

**Step 3: Start Infrastructure Services**
You still need PostgreSQL and Redis. Use Docker to quickly spin them up:
```bash
# This will only start the database and cache, leaving the bot for you to run locally
docker-compose up -d postgres redis
```

**Step 4: Install and Prepare Ollama (For Q/A Pipeline)**
Ollama powers the local AI components. 
```bash
# Install Ollama (Linux/macOS)
curl -fsSL https://ollama.ai/install.sh | sh

# Pull the language model required for analysis
ollama pull gemma3:12b
```

**Step 5: Run the Bot**
With the database running and dependencies installed, you can now run the pipeline — see **[Running and Stopping the Pipeline](#running-and-stopping-the-pipeline)** for the three supported ways (CLI foreground, CLI background, Dashboard).

---

## Running and Stopping the Pipeline

The pipeline (`python -m src.main run`) loops forever: **scrape → dedup → Q/A → wait `round_delay_seconds` → repeat**. There are three ways to start/stop it.

### Option 1 — Dashboard (easiest, no terminal management)

The dashboard at `http://localhost:5050` has a **Pipeline** tab with **Start / Stop / Run single round** buttons and a live log tail. A `Pipeline` status pill in the top bar shows current state from every tab. The dashboard spawns the pipeline as a detached subprocess; the PID is persisted to `data/pipeline.pid`, so closing the dashboard does not stop the pipeline.

```bash
# Start the dashboard
source venv/bin/activate
python -m src.main dashboard         # http://localhost:5050
# Then click Start / Stop in the Pipeline tab.
```

REST equivalents (also usable from `curl`/scripts):

```bash
curl -X POST http://localhost:5050/api/pipeline/start
curl -X POST http://localhost:5050/api/pipeline/stop
curl -X POST http://localhost:5050/api/pipeline/run-single
curl       http://localhost:5050/api/pipeline/status
curl       http://localhost:5050/api/pipeline/logs?lines=200
```

### Option 2 — CLI in the foreground

Best for quick tests. The pipeline holds the terminal; you watch logs live.

```bash
source venv/bin/activate
python -m src.main run             # continuous mode, 7/24
# … logs stream here …
# Stop with Ctrl+C (graceful: pipeline finishes the current scrape and cleans up).
```

Single round (one scrape → dedup → Q/A pass, then exit):
```bash
python -m src.main single-round
```

### Option 3 — CLI in the background (nohup)

For headless servers when the dashboard is overkill.

```bash
# Start
source venv/bin/activate
nohup python -m src.main run > logs/pipeline.out 2>&1 &
echo $! > data/pipeline.pid           # save PID for later

# Watch
tail -f logs/pipeline.out

# Stop (graceful)
kill -TERM "$(cat data/pipeline.pid)"
# … if it doesn't exit within ~15 s …
kill -KILL "$(cat data/pipeline.pid)"
rm data/pipeline.pid
```

### Option 4 — Docker Compose (production)

```bash
docker-compose up -d                  # start everything (collector + db + redis)
docker-compose logs -f collector      # follow logs
docker-compose stop collector         # graceful stop
docker-compose start collector        # resume
docker-compose down                   # stop and remove containers
```

### How often does the pipeline run?

- One **round** = one full scrape → dedup → Q/A pass across all enabled sources.
- Between rounds it sleeps for `general.round_delay_seconds` (default **43200 = 12 h**) — see `config/settings.yaml`.
- Q/A generation only starts when there are `pipeline.stages[generate_qa].min_documents_to_start` (default **100**) unique documents in the DB.
- Each source also has its own `scrape_interval_hours` for rate-limiting.

### Troubleshooting

- **`Address already in use` on port 5000** — macOS uses port 5000 for **AirPlay Receiver**. The dashboard now defaults to **5050**. To free 5000: System Settings → General → AirDrop & Handoff → AirPlay Receiver → Off.
- **`python -m src.main run` exits immediately** — check `python -m src.main health`; usually means Postgres or Redis is not reachable. Run `docker-compose up -d postgres redis`.
- **Q/A stays at 0** — verify (a) Ollama is up (`curl http://localhost:11434/api/tags`), (b) the configured model is pulled (`ollama list`), (c) unique docs ≥ 100. Force a manual run with `python -m src.main generate-qa --batch-size 20`.
- **Pipeline subprocess from dashboard not stopping** — `kill -TERM "$(cat data/pipeline.pid)"`, then `rm data/pipeline.pid` if needed. The dashboard also exposes `POST /api/pipeline/stop`.

---

### Option C: Bare-Metal Linux Server Production Deployment

If you are deploying this on a fresh Ubuntu/Debian server for long-term scraping:

**Step 1: Run the Install Script**
```bash
sudo bash scripts/install.sh
```

**Step 2: Mount External Storage (Optional but Recommended)**
```bash
# Example: Mounting a secondary drive to the data directory
sudo mount /dev/sdb1 /data/car-collector
echo '/dev/sdb1 /data/car-collector ext4 defaults 0 2' | sudo tee -a /etc/fstab
```

**Step 3: Start via Systemd**
```bash
sudo systemctl enable datacollector
sudo systemctl start datacollector
sudo systemctl status datacollector
```

## CLI Commands

```bash
# Continuous pipeline (7/24)
python -m src.main run

# Single round
python -m src.main single-round

# Scraping only (no Q/A generation)
python -m src.main scrape-only
python -m src.main scrape-only --source reddit_mechanicadvice

# Q/A generation
python -m src.main generate-qa --batch-size 20

# Export Q/A
python -m src.main export-qa --output training_data.jsonl --format jsonl
python -m src.main export-qa --output training_data.json --format json

# Statistics
python -m src.main stats

# Health check
python -m src.main health

# List sources
python -m src.main list-sources
```

## Dashboard

Access the real-time dashboard at **`http://localhost:5050`** (Docker maps it to `http://localhost:5001`).

Tabs:
- **Overview** — totals, 14-day activity chart, source distribution
- **Sources** — per-source totals, unique counts, Q/A pairs, last scrape
- **Documents** — searchable, filterable browser (by source, with/without Q/A, full-text search). Click a row for the full content and its Q/A pairs.
- **Q/A Pairs** — paginated list with filters; on-demand Q/A generation against any batch size
- **Rounds** — last 20 rounds with scrape/dedup/Q/A counts and error totals
- **Pipeline** — **Start / Stop / Run single round** controls + live log tail + Q/A export to `data/qa_output/`
- **System** — Postgres, Redis, and Ollama health, sizes, and model list

The top bar shows a status pill for the pipeline subprocess and quick Start/Stop buttons available from every tab.

### Useful REST endpoints

```
GET  /api/stats                       # overview counts + disk
GET  /api/sources                     # per-source totals
GET  /api/data?source=…&search=…      # paginated documents
GET  /api/data/<doc_id>               # one document + its Q/A
GET  /api/qa?brand=…&search=…         # paginated Q/A pairs
GET  /api/qa/stats                    # avg lengths, per-source, per-brand
POST /api/qa/generate                 # trigger Q/A generation
GET  /api/system/status               # postgres / redis / ollama checks
GET  /api/pipeline/status             # pipeline subprocess state
POST /api/pipeline/start              # start `src.main run`
POST /api/pipeline/stop               # SIGTERM (then SIGKILL after grace)
POST /api/pipeline/run-single         # one-shot single-round
POST /api/pipeline/export-qa          # export to data/qa_output/
GET  /api/pipeline/logs?lines=200     # tail dashboard-spawned pipeline log
```

## Configuration

All settings in `config/settings.yaml`:

### Scraping Intervals

```yaml
sources:
  - name: "reddit_mechanicadvice"
    rate_limit: 2.0              # Seconds between requests
    scrape_interval_hours: 6     # How often to revisit
    max_pages_per_round: 500     # Max pages per scrape round
```

### Cloudflare Bypass

```yaml
cloudflare:
  enabled: true
  use_playwright: true           # JS rendering
  use_cloudscraper: true         # Fallback
  min_delay: 2.0
  max_delay: 5.0
```

### Adding New Sources

```yaml
sources:
  - name: "new_source"
    type: "forum"               # forum, technical, qa, content
    enabled: true
    base_url: "https://example.com"
    start_urls:
      - "https://example.com/forum/"
    priority: 2
    rate_limit: 5.0
    scrape_interval_hours: 12
    max_pages_per_round: 300
    use_playwright: true        # For JS-heavy sites
    cloudflare_protected: true  # Enable bypass
    selectors:
      thread_list: "a.topic-title"
      thread_title: "h1"
      post_content: "div.post-body"
      pagination_next: "a.next"
```

## Pipeline Flow

```
1. SCRAPE (Parallel per source)
   ├── Playwright stealth mode for JS sites
   ├── Cloudflare bypass (cloudscraper fallback)
   ├── Rate limiting per source config
   └── Extract threads, posts, articles

2. STORE
   ├── PostgreSQL for metadata
   ├── HTML files to disk
   └── Redis cache for duplicates

3. DEDUPLICATE
   ├── Layer 1: SHA-256 exact match
   ├── Layer 2: SimHash near-duplicate
   └── Layer 3: LLM quality scoring

4. GENERATE Q/A
   ├── Send unique docs to Ollama
   ├── Generate Q/A pairs
   └── Store with quality scores

5. WAIT (interval) → REPEAT
```

## Q/A Output Format

JSONL format (one record per line):

```json
{"question": "What causes BMW 328i rough idle at cold start?", "answer": "Common causes include...", "source": "reddit_mechanicadvice", "car_brand": "BMW", "car_model": "328i", "quality_score": 0.85}
```

## Directory Structure

```
datacollectorbot/
├── config/
│   └── settings.yaml           # Main configuration
├── src/
│   ├── main.py                 # CLI entry point
│   ├── models/                 # Pydantic data models
│   ├── database/               # PostgreSQL management
│   ├── scrapers/
│   │   ├── base_scraper.py     # Base scraper (abstract)
│   │   ├── forum_scraper.py    # Forum, QA, Content scrapers
│   │   └── bypass/             # Cloudflare bypass module
│   │       └── cloudflare.py
│   ├── dashboard/              # Flask web dashboard
│   │   ├── app.py
│   │   └── templates/
│   ├── storage/                # File storage
│   ├── dedup/                  # Duplicate detection
│   ├── qa_generator/           # LLM Q/A generator
│   ├── pipeline/               # Pipeline orchestrator
│   └── utils/                  # Helper utilities
├── scripts/
│   └── init-db.sql             # PostgreSQL schema
├── systemd/
│   └── datacollector.service   # Systemd service file
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## License

Internal use only.
